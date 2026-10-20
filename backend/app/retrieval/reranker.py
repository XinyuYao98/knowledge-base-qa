"""Reranker：对 RRF 候选做逐对精排。

## 为什么 RRF 之后还要 rerank

RRF 只用排名，不看语义。它能选出「两路都提到」的候选，
但分不清「哪个真的回答了问题」。

实测参照：P4 的竖切片里，向量 Top1 相似度 0.828、第二名 0.605 ——
分差大时 RRF 够用。但当多个候选都在 0.5-0.6 之间时，
RRF 的排名几乎等同于随机，必须靠语义重排。

## 双协议适配（实测需要）

Reranker 的调用协议不统一：

| 服务 | 协议 |
|---|---|
| vLLM / TEI | `POST /v1/rerank`，返回 `results: [{index, relevance_score}]` |
| **LM Studio** | 多数情况下没有 `/v1/rerank`，只能用 chat + 提示词打分 |

所以默认 `auto`：先试 endpoint，失败回退 chat。
**这个回退不是「优雅降级」，而是「LM Studio 下的唯一可用路径」。**

## chat 打分的两个坑

### 坑 1：不能逐对串行

20 条候选串行调用 = 20 次 HTTP 往返。按每次 300ms 算就是 6 秒，
远超架构的 5 秒超时预算。

用 `asyncio.gather` 并发（`RERANK_CONCURRENCY`），20 条降到约 1.5 秒。

### 坑 2：模型可能不输出纯数字

`qwen3-reranker` 的 chat 模式常输出「相关度：0.85」或「该段落很相关」。
必须容错解析：抓第一个 0-1 之间的浮点数，抓不到就给中性分 0.5
（而不是给 0 或 1 —— 那会把候选推到极端位置）。
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("reranker")

#: chat 打分的提示词。
#:
#: 要求「只输出一个数字」是刻意的：越短越容易被约束。
#: 但仍要容错 —— 实测模型经常加前缀。
SCORE_PROMPT = """判断下面这段资料能否回答用户的问题。

用户问题：{question}

资料：{passage}

只输出一个 0 到 1 之间的小数表示相关性，不要输出任何其他内容。
能直接回答为 1，完全无关为 0。"""

#: 抓模型输出里的第一个数字（整数或小数，可带前导点）。
#:
#: **不能只匹配 0/1 开头的数** —— 模型会用 0-10 或 0-100 分制，
#: `8.5` 和 `85` 也要先抓到，再按量级归一。
#: 第一版就是因为正则限死 `[01]` 开头，导致这两种输出全被当成「抓不到」。
_SCORE_RE = re.compile(r"(\d*\.?\d+)")

#: 抓不到数字时给的中性分。
#:
#: 为什么不给 0：候选能进 RRF 前 20，说明至少一路认为它相关，
#: 给 0 会让它排到最末，等于丢掉这条路径的价值。
#: 为什么不给 1：那会让它排到最前，同样是失真。
NEUTRAL_SCORE = 0.5


@dataclass(slots=True)
class RerankHit:
    chunk_id: str
    score: float
    original_rank: int


@dataclass(slots=True)
class RerankResult:
    hits: list[RerankHit]
    protocol: str = ""             # endpoint / chat / skipped
    duration_ms: float = 0.0
    degraded: str = ""
    timings: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "duration_ms": round(self.duration_ms, 1),
            "degraded": self.degraded,
            "top": [
                {"chunk_id": h.chunk_id, "score": round(h.score, 4)} for h in self.hits[:5]
            ],
        }


def parse_score(raw: str) -> float:
    """从模型输出里解析分数。

    实测的几种输出形态：
        "0.85"            → 0.85
        "相关度：0.85"     → 0.85
        ".9"              → 0.9
        "1"               → 1.0
        "无法判断"         → 0.5（中性）
    """
    if not raw:
        return NEUTRAL_SCORE
    text = raw.strip()

    # 百分比形态：85%
    pct = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    if pct:
        return min(1.0, max(0.0, float(pct.group(1)) / 100))

    # 优先取「像分数」的数：带小数点、或本身就是 0/1。
    # 这样「第 3 条资料的相关度是 0.85」会取 0.85 而不是 3。
    candidates = _SCORE_RE.findall(text)
    if not candidates:
        return NEUTRAL_SCORE

    v: float | None = None
    for raw_num in candidates:
        try:
            num = float(raw_num)
        except ValueError:
            continue
        if num <= 1.0:
            v = num
            break
    if v is None:
        # 没有 0-1 范围的数，取第一个，后面按量级归一
        try:
            v = float(candidates[0])
        except ValueError:
            return NEUTRAL_SCORE

    if v < 0:
        return 0.0
    if v > 1:
        # 模型偶尔输出 0-100 或 0-10 的分制，按量级归一
        if v <= 10:
            v = v / 10
        elif v <= 100:
            v = v / 100
        else:
            return 1.0
    return round(v, 4)


class Reranker:
    """逐对精排。"""

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.base_url = (base_url or settings.RERANK_BASE_URL).rstrip("/")
        self.model = model or settings.RERANK_MODEL
        self.api_key = api_key or settings.RERANK_API_KEY
        self._detected_protocol: str | None = None

    def _client(self, timeout: float | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=httpx.Timeout(timeout or settings.RERANK_TIMEOUT_SECONDS, connect=5.0),
            # 本地服务必须绕开系统代理（P4 踩过的坑）
            trust_env=not settings.LLM_DISABLE_PROXY,
        )

    async def rerank(self, question: str, candidates: list[tuple[str, str]]) -> RerankResult:
        """对候选重排。

        `candidates`：`[(chunk_id, text), ...]`，顺序即 RRF 排名。

        **永不抛异常** —— 失败就按原顺序返回，标 degraded。
        """
        t0 = time.perf_counter()

        if not settings.RERANK_ENABLED:
            return RerankResult(
                hits=[RerankHit(cid, 1.0 / (i + 1), i + 1) for i, (cid, _) in enumerate(candidates)],
                protocol="skipped",
                degraded="rerank_disabled",
                duration_ms=(time.perf_counter() - t0) * 1000,
            )

        if not candidates:
            return RerankResult(hits=[], protocol="skipped")

        # 先试 endpoint（若已探测过就直接用探测结果）
        proto = settings.RERANK_PROTOCOL
        if proto in ("auto", "endpoint") and self._detected_protocol != "chat":
            try:
                hits = await self._rerank_endpoint(question, candidates)
                self._detected_protocol = "endpoint"
                return RerankResult(
                    hits=hits,
                    protocol="endpoint",
                    duration_ms=(time.perf_counter() - t0) * 1000,
                )
            except Exception as e:
                if proto == "endpoint":
                    log.warning("rerank_endpoint_failed", err=type(e).__name__)
                    return self._fallback(candidates, "endpoint_unavailable", t0)
                # auto 模式：回退 chat
                log.info("rerank_fallback_to_chat", reason=type(e).__name__)
                self._detected_protocol = "chat"

        try:
            hits = await self._rerank_chat(question, candidates)
            return RerankResult(
                hits=hits,
                protocol="chat",
                duration_ms=(time.perf_counter() - t0) * 1000,
            )
        except Exception as e:
            log.warning("rerank_chat_failed", err=type(e).__name__)
            return self._fallback(candidates, "rerank_unavailable", t0)

    # ------------------------------------------------------------------
    async def _rerank_endpoint(
        self, question: str, candidates: list[tuple[str, str]]
    ) -> list[RerankHit]:
        """标准 /v1/rerank 协议（vLLM / TEI）。"""
        async with self._client() as c:
            resp = await c.post(
                "/rerank",
                json={
                    "model": self.model,
                    "query": question,
                    "documents": [text for _, text in candidates],
                    "top_n": len(candidates),
                },
            )
        if resp.status_code == 404:
            raise RuntimeError("no_rerank_endpoint")
        if resp.status_code != 200:
            raise RuntimeError(f"rerank_http_{resp.status_code}")

        data = resp.json()
        results = data.get("results", [])
        if not results:
            raise RuntimeError("rerank_empty")

        out: list[RerankHit] = []
        for r in results:
            idx = int(r["index"])
            out.append(
                RerankHit(
                    chunk_id=candidates[idx][0],
                    score=float(r.get("relevance_score", 0.0)),
                    original_rank=idx + 1,
                )
            )
        out.sort(key=lambda h: -h.score)
        return out

    async def _rerank_chat(
        self, question: str, candidates: list[tuple[str, str]]
    ) -> list[RerankHit]:
        """chat + 提示词打分（LM Studio 形态）。并发执行。"""
        sem = asyncio.Semaphore(settings.RERANK_CONCURRENCY)

        async def score_one(idx: int, text: str) -> RerankHit:
            async with sem:
                prompt = SCORE_PROMPT.format(question=question, passage=text[:1500])
                async with self._client() as c:
                    resp = await c.post(
                        "/chat/completions",
                        json={
                            "model": self.model,
                            "messages": [{"role": "user", "content": prompt}],
                            "max_tokens": 8,
                            "temperature": 0.0,
                        },
                    )
                if resp.status_code != 200:
                    raise RuntimeError(f"chat_http_{resp.status_code}")
                content = resp.json()["choices"][0]["message"]["content"]
                return RerankHit(
                    chunk_id=candidates[idx][0],
                    score=parse_score(content),
                    original_rank=idx + 1,
                )

        hits = await asyncio.gather(
            *[score_one(i, t) for i, (_, t) in enumerate(candidates)]
        )
        result = list(hits)
        # 稳定排序：同分时保持原 RRF 顺序（RRF 已经是一个有序先验）
        result.sort(key=lambda h: (-h.score, h.original_rank))
        return result

    @staticmethod
    def _fallback(
        candidates: list[tuple[str, str]], reason: str, t0: float
    ) -> RerankResult:
        """降级：保留原顺序，分数用倒数排名。

        **保留原顺序而不是随机** —— RRF 的顺序本身已经是有信息量的，
        比随机或全零分更好。
        """
        return RerankResult(
            hits=[
                RerankHit(cid, 1.0 / (i + 1), i + 1) for i, (cid, _) in enumerate(candidates)
            ],
            protocol="fallback",
            degraded=reason,
            duration_ms=(time.perf_counter() - t0) * 1000,
        )


_instance: Reranker | None = None


def get_reranker() -> Reranker:
    global _instance
    if _instance is None:
        _instance = Reranker()
    return _instance
