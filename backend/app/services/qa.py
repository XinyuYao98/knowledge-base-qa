"""问答编排：检索 → 组装上下文 → 生成 → 引用校验。

## 流程（架构 §6.4）

```
问题
  └→ Retriever（改写 → 双路召回 → RRF → rerank）
       └→ format_context（带编号 [1][2]）
            └→ LLM 生成（grounding prompt）
                 └→ 引用校验
                      ├─ 通过 → 返回
                      └─ 失败 → 修复一次 → 仍失败则标注「无引用」但保留答案
```

## 为什么「引用校验失败」不直接丢弃答案

三种处理方式：

| 方案 | 后果 |
|---|---|
| 丢弃重来 | 用户等了两倍时间还是没答案 |
| 原样返回 | 无法追溯，失去 RAG 的意义 |
| **修复一次，仍失败则标注** | 用户拿到答案，同时也知道「这条没出处」 |

选第三种。**关键是不能让用户以为无引用 = 有引用**。

## 为什么只修复一次

修复本身要再跑一次生成（2-4 秒）。修不好就说明模型这轮状态不好，
再试第三次大概率还是失败，只是让用户多等。

## 停顿与降级

| 环节 | 失败处理 |
|---|---|
| 检索为空 | 直接回「资料中未提及」，**不调 LLM**（省 3 秒且避免编造） |
| 生成失败 | 抛 503，让前端提示「模型服务不可用」 |
| 引用校验异常 | 视为通过，只记日志（不该因校验逻辑 bug 阻断主链路） |
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.rag.citations import (
    CitationCheck,
    check_citations,
    format_context,
    is_refusal,
)
from app.rag.models import ChatClient, ModelServiceError
from app.rag.prompts.qa import (
    PROMPT_VERSION,
    REPAIR_INSTRUCTION,
    SYSTEM_PROMPT,
    build_user_prompt,
)
from app.retrieval.retriever import Candidate, RetrievalResult, Retriever

log = get_logger("qa")

#: 检索无结果时的固定回复。
#:
#: 刻意与提示词里的拒答短语一致（`资料中未提及`），
#: 让前端可以用同一个判断逻辑处理「拒答」。
NO_CONTEXT_ANSWER = "资料中未提及（当前知识库没有检索到相关内容）"


@dataclass(slots=True)
class Answer:
    answer: str
    citations: list[dict[str, Any]] = field(default_factory=list)
    caveat: str = ""                 # 无引用等提示，供前端显著展示
    retrieval: RetrievalResult | None = None
    citation_check: dict[str, Any] = field(default_factory=dict)
    repaired: bool = False
    duration_ms: float = 0.0
    timings: dict[str, float] = field(default_factory=dict)
    degraded: list[str] = field(default_factory=list)
    prompt_version: str = PROMPT_VERSION
    model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "citations": self.citations,
            "caveat": self.caveat,
            "repaired": self.repaired,
            "duration_ms": round(self.duration_ms, 1),
            "timings": {k: round(v, 1) for k, v in self.timings.items()},
            "degraded": self.degraded,
            "prompt_version": self.prompt_version,
            "model": self.model,
            "retrieval": self.retrieval.to_dict() if self.retrieval else None,
            "citation_check": self.citation_check,
        }


def _candidate_to_item(c: Candidate) -> dict[str, Any]:
    """把候选转成 `format_context` 需要的条目。

    label 的构造直接决定引用可读性 —— 用户要能凭这个找到原文。
    """
    meta = c.metadata or {}
    parts: list[str] = []

    if fname := meta.get("filename"):
        parts.append(str(fname))

    if sheet := meta.get("sheet"):
        parts.append(f"表 {sheet}")
        if row := meta.get("row"):
            parts.append(f"第 {row} 行")
    elif page := meta.get("page"):
        parts.append(f"第 {page} 页")

    if hp := meta.get("heading_path"):
        parts.append(str(hp))

    label = " · ".join(parts) if parts else "资料"

    return {
        "chunk_id": c.chunk_id,
        "text": c.text,
        "label": label,
        "metadata": meta,
        "rrf_score": c.rrf_score,
        "sources": c.sources,
    }


class Answerer:
    """问答编排。"""

    def __init__(
        self,
        retriever: Retriever | None = None,
        chat: ChatClient | None = None,
    ) -> None:
        self.retriever = retriever or Retriever()
        self.chat = chat or ChatClient()

    async def answer(
        self,
        kb_id: str | uuid.UUID,
        question: str,
        *,
        user_id: str | uuid.UUID | None = None,
        history: list[dict[str, str]] | None = None,
        turn_count: int = 0,
    ) -> Answer:
        started = time.perf_counter()
        timings: dict[str, float] = {}
        degraded: list[str] = []

        # ---- 1. 检索 ----
        t0 = time.perf_counter()
        retrieval = await self.retriever.retrieve(
            kb_id, question, user_id=user_id, history=history, turn_count=turn_count
        )
        timings["retrieve"] = (time.perf_counter() - t0) * 1000
        degraded.extend(retrieval.degraded)

        if retrieval.empty:
            # 不调 LLM：省 3 秒，且避免模型对着空资料编造
            log.info("qa_no_context", kb_id=str(kb_id))
            return Answer(
                answer=NO_CONTEXT_ANSWER,
                retrieval=retrieval,
                duration_ms=(time.perf_counter() - started) * 1000,
                timings=timings,
                degraded=degraded,
                model=settings.CHAT_MODEL,
            )

        # ---- 2. 组装上下文 ----
        items = [_candidate_to_item(c) for c in retrieval.candidates]
        context, used = format_context(items, max_chars=settings.CONTEXT_MAX_CHARS)
        if len(used) < len(items):
            degraded.append("context_truncated")

        user_prompt = build_user_prompt(context, retrieval.question)
        citations_out = [_citation_payload(i, u) for i, u in enumerate(used, start=1)]

        # ---- 3. 生成 ----
        t0 = time.perf_counter()
        try:
            result = await self.chat.complete(
                system=SYSTEM_PROMPT,
                user=user_prompt,
                max_tokens=settings.ANSWER_MAX_TOKENS,
            )
        except ModelServiceError as e:
            timings["generate"] = (time.perf_counter() - t0) * 1000
            log.error("qa_generate_failed", err=e.message[:120])
            raise

        timings["generate"] = (time.perf_counter() - t0) * 1000
        answer_text = (result.get("content") or "").strip()

        # ---- 4. 引用校验 ----
        t0 = time.perf_counter()
        check = check_citations(answer_text, available=len(used))
        repaired = False
        caveat = ""

        if not check.is_valid and not is_refusal(answer_text):
            repaired = True
            fixed = await self._repair(check, retrieval.question, user_prompt)
            if fixed is not None:
                answer_text = fixed
                check = check_citations(answer_text, available=len(used))
            if not check.is_valid:
                # 修不好就保留答案，但必须显著标注
                reason = _describe_issue(check)
                caveat = f"本条回答未能标注有效来源，请谨慎参考并自行核对原文。（{reason}）"
                degraded.append("citation_unverified")
                log.warning("citation_unverified", reason=reason)

        timings["citation_check"] = (time.perf_counter() - t0) * 1000

        return Answer(
            answer=answer_text,
            citations=citations_out,
            caveat=caveat,
            retrieval=retrieval,
            citation_check=check.to_dict(),
            repaired=repaired,
            duration_ms=(time.perf_counter() - started) * 1000,
            timings=timings,
            degraded=degraded,
            model=settings.CHAT_MODEL,
        )

    async def _repair(
        self, check: CitationCheck, question: str, _original_prompt: str
    ) -> str | None:
        """引用不合格时重生成一次。失败返回 None。"""
        try:
            result = await self.chat.complete(
                system=SYSTEM_PROMPT,
                user=REPAIR_INSTRUCTION.format(
                    reason=_describe_issue(check),
                    available=_available_count(check),
                    question=question,
                ),
                max_tokens=settings.ANSWER_MAX_TOKENS,
            )
        except ModelServiceError as e:
            log.warning("qa_repair_failed", err=e.message[:100])
            return None
        text = (result.get("content") or "").strip()
        return text or None

    # ------------------------------------------------------------------
    async def stream_answer(
        self,
        kb_id: str | uuid.UUID,
        question: str,
        *,
        user_id: str | uuid.UUID | None = None,
        history: list[dict[str, str]] | None = None,
        turn_count: int = 0,
    ) -> AsyncIterator[dict[str, Any]]:
        """SSE 流式问答。

        事件序列：

            meta   → 检索完成，带引用列表（前端可先渲染出处）
            delta  → 文本增量
            done   → 结束，带校验结果与耗时
            error  → 出错

        **为什么先发 meta**：引用在生成之前就确定了（来自检索），
        先发能让前端立刻展示「依据这些资料」，用户感知更快。
        """
        started = time.perf_counter()

        retrieval = await self.retriever.retrieve(
            kb_id, question, user_id=user_id, history=history, turn_count=turn_count
        )

        if retrieval.empty:
            yield {"event": "meta", "data": {"citations": [], "empty": True}}
            yield {"event": "delta", "data": {"text": NO_CONTEXT_ANSWER}}
            yield {
                "event": "done",
                "data": {
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    "degraded": retrieval.degraded,
                    "citation_ok": True,
                },
            }
            return

        items = [_candidate_to_item(c) for c in retrieval.candidates]
        context, used = format_context(items, max_chars=settings.CONTEXT_MAX_CHARS)
        citations_out = [_citation_payload(i, u) for i, u in enumerate(used, start=1)]

        yield {
            "event": "meta",
            "data": {
                "citations": citations_out,
                "question": retrieval.question,
                "rewritten": retrieval.rewrite.rewritten if retrieval.rewrite else False,
                "retrieve_ms": round(retrieval.duration_ms, 1),
            },
        }

        user_prompt = build_user_prompt(context, retrieval.question)
        pieces: list[str] = []

        try:
            async for chunk in self.chat.stream(
                system=SYSTEM_PROMPT,
                user=user_prompt,
                max_tokens=settings.ANSWER_MAX_TOKENS,
            ):
                pieces.append(chunk)
                yield {"event": "delta", "data": {"text": chunk}}
        except ModelServiceError as e:
            yield {"event": "error", "data": {"message": e.message}}
            return

        full = "".join(pieces).strip()

        # 流式下无法「重生成」—— 用户已经看到内容了。
        # 所以这里只做校验并告知，不改写答案。
        check = check_citations(full, available=len(used))
        caveat = ""
        if not check.is_valid and not is_refusal(full):
            reason = _describe_issue(check)
            caveat = f"本条回答未能标注有效来源（{reason}），请谨慎参考。"
            log.warning("stream_citation_unverified", reason=reason)

        yield {
            "event": "done",
            "data": {
                "answer": full,
                "citations": citations_out,
                "citation_ok": check.is_valid,
                "caveat": caveat,
                "repaired": False,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "timings": {k: round(v, 1) for k, v in retrieval.timings.items()},
                "degraded": retrieval.degraded,
                "prompt_version": PROMPT_VERSION,
                "model": settings.CHAT_MODEL,
            },
        }


def _describe_issue(check: CitationCheck) -> str:
    """把校验结果转成人能看懂的一句话。

    `CitationCheck` 只提供结构化的 cited / invalid / unused，
    没有现成的中文描述（P6 实测发现我一开始假设有 `.reason`，实际没有）。
    """
    parts: list[str] = []
    if check.invalid:
        parts.append(f"引用了不存在的编号 {check.invalid}")
    if not check.cited:
        parts.append("回答中没有任何引用")
    return "；".join(parts) or "引用不完整"


def _available_count(check: CitationCheck) -> int:
    """可用编号的上界。

    `CitationCheck` 不直接提供「一共给了几条资料」，
    但 `unused` + `cited` 的并集就是全部有效编号范围。
    """
    nums = set(check.cited) | set(check.unused)
    return max(nums) if nums else 0


def _citation_payload(idx: int, used_item: dict[str, Any]) -> dict[str, Any]:
    """生成前端可用的引用结构。

    前端点开引用要能跳到原文，所以必须带：
    - `index`：与答案里的 `[n]` 对应
    - `chunk_id` / `document_id`：定位
    - `label`：人可读的出处描述
    - `preview`：悬停预览
    """
    meta = used_item.get("metadata") or {}
    text = (used_item.get("text") or "").strip()
    return {
        "index": idx,
        "chunk_id": used_item.get("chunk_id"),
        "document_id": meta.get("document_id"),
        "label": used_item.get("label") or "资料",
        "preview": text[:200],
        "metadata": {
            "page": meta.get("page"),
            "sheet": meta.get("sheet"),
            "row": meta.get("row"),
            "heading_path": meta.get("heading_path"),
            "filename": meta.get("filename"),
        },
    }
