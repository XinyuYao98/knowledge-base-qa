"""检索编排：改写 → 双路召回 → RRF 融合。

## 流程（架构 D1 + D13）

```
用户问题
  ├── 判断是否改写（命中指代词 且 轮次>1）
  │     └── 是 → LLM 改写 → 用改写后的问题检索
  │
  ├── 并行两路召回
  │     ├── 向量：Chroma cosine，top 20
  │     └── BM25：jieba 分词，top 20
  │
  └── RRF 融合 → top 20（送 reranker）
```

## 为什么要并行

两路召回互不依赖。串行做等于把延迟相加（向量 ~800ms + BM25 ~10ms），
并行后只取较慢的那个。

BM25 是纯内存计算（实测 403 块 2.5ms），基本不占时间；
真正的耗时在向量的 HTTP 调用上。

## 降级策略

| 环节失败 | 处理 |
|---|---|
| 改写失败 | 用原问题，标 `degraded=no_rewrite` |
| BM25 无结果 | 只用向量结果（RRF 自然处理单路） |
| 向量无结果 | 只用 BM25 结果 |
| 两路都空 | 返回空 + 明确标志，由上层决定「无资料」回复 |
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.index import chroma_store
from app.rag.models import EmbeddingClient, ModelServiceError
from app.retrieval.bm25 import get_manager
from app.retrieval.reranker import RerankResult, get_reranker
from app.retrieval.rewriter import RewriteResult, rewrite
from app.retrieval.rrf import FusedHit, fuse

log = get_logger("retrieval")


@dataclass(slots=True)
class Candidate:
    """融合后的候选，带原文与元数据（rerank 与生成都要用）。"""

    chunk_id: str
    text: str
    rrf_score: float
    hit_count: int
    sources: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "rrf_score": round(self.rrf_score, 6),
            "hit_count": self.hit_count,
            "sources": self.sources,
            "text": self.text[:120],
        }


@dataclass(slots=True)
class RetrievalResult:
    question: str                    # 实际用于检索的问题（可能是改写后的）
    original_question: str
    candidates: list[Candidate]
    rewrite: RewriteResult | None = None
    rerank: RerankResult | None = None
    vector_count: int = 0
    bm25_count: int = 0
    fused_count: int = 0
    duration_ms: float = 0.0
    timings: dict[str, float] = field(default_factory=dict)
    degraded: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.candidates

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "original_question": self.original_question,
            "candidates": [c.to_dict() for c in self.candidates],
            "rewrite": self.rewrite.to_dict() if self.rewrite else None,
            "rerank": self.rerank.to_dict() if self.rerank else None,
            "vector_count": self.vector_count,
            "bm25_count": self.bm25_count,
            "fused_count": self.fused_count,
            "duration_ms": round(self.duration_ms, 1),
            "timings": {k: round(v, 1) for k, v in self.timings.items()},
            "degraded": self.degraded,
        }


class Retriever:
    """双路检索器。"""

    def __init__(
        self,
        embedder: EmbeddingClient | None = None,
        reranker: Any | None = None,
    ) -> None:
        self.embedder = embedder or EmbeddingClient()
        self.reranker = reranker or get_reranker()
        self.bm25 = get_manager()

    async def retrieve(
        self,
        kb_id: str | uuid.UUID,
        question: str,
        *,
        user_id: str | uuid.UUID | None = None,
        history: list[dict[str, str]] | None = None,
        turn_count: int = 0,
        final_top_k: int | None = None,
    ) -> RetrievalResult:
        """完整检索流程。**失败不抛异常**，降级返回已有结果。"""
        started = time.perf_counter()
        original = question
        timings: dict[str, float] = {}
        degraded: list[str] = []

        # ---- 1. 按需改写 ----
        t0 = time.perf_counter()
        rw = await rewrite(question, history, turn_count=turn_count)
        timings["rewrite"] = (time.perf_counter() - t0) * 1000
        if rw.degraded:
            degraded.append(rw.degraded)
        question = rw.question

        # ---- 2. 并行双路召回 ----
        t0 = time.perf_counter()
        vec_task = asyncio.create_task(self._vector_recall(kb_id, question))
        bm25_task = asyncio.create_task(self._bm25_recall(kb_id, question))

        vec_results, bm25_results = await asyncio.gather(
            vec_task, bm25_task, return_exceptions=True
        )
        timings["recall"] = (time.perf_counter() - t0) * 1000

        if isinstance(vec_results, Exception):
            log.warning("vector_recall_failed", err=type(vec_results).__name__)
            degraded.append("vector_unavailable")
            vec_results = []
        if isinstance(bm25_results, Exception):
            log.warning("bm25_recall_failed", err=type(bm25_results).__name__)
            degraded.append("bm25_unavailable")
            bm25_results = []

        # ---- 3. 融合 ----
        t0 = time.perf_counter()
        rankings: dict[str, list[tuple[str, float]]] = {}
        if vec_results:
            rankings["vector"] = [(h["id"], h["score"]) for h in vec_results]
        if bm25_results:
            rankings["bm25"] = [(h.chunk_id, h.score) for h in bm25_results]

        fused: list[FusedHit] = fuse(
            rankings, limit=settings.RETRIEVE_FUSE_TOP_K
        ) if rankings else []
        timings["fuse"] = (time.perf_counter() - t0) * 1000

        # ---- 4. 补全原文 ----
        t0 = time.perf_counter()
        candidates = await self._hydrate(fused, vec_results, kb_id, user_id)
        timings["hydrate"] = (time.perf_counter() - t0) * 1000

        # ---- 5. 精排 ----
        # RRF 只看排名不看语义，用它选出候选；最后用 reranker 定顺序。
        # 只在候选少于最终要的条数时跳过 —— 没必要重排 3 条取 5 条。
        rerank_result: RerankResult | None = None
        target = final_top_k if final_top_k is not None else settings.RETRIEVE_FINAL_TOP_K
        if candidates and len(candidates) > target:
            t0 = time.perf_counter()
            rerank_result = await self.reranker.rerank(
                question, [(c.chunk_id, c.text) for c in candidates]
            )
            timings["rerank"] = (time.perf_counter() - t0) * 1000
            if rerank_result.degraded:
                degraded.append(rerank_result.degraded)

            # 按 rerank 分数重排
            order = {h.chunk_id: i for i, h in enumerate(rerank_result.hits)}
            candidates.sort(key=lambda c: order.get(c.chunk_id, len(order)))

        candidates = candidates[:target]

        total = (time.perf_counter() - started) * 1000
        log.info(
            "retrieve_done",
            kb_id=str(kb_id),
            vec=len(vec_results),
            bm25=len(bm25_results),
            fused=len(fused),
            ms=round(total, 1),
            rewritten=rw.rewritten,
        )

        return RetrievalResult(
            question=question,
            original_question=original,
            candidates=candidates,
            rewrite=rw,
            rerank=rerank_result,
            vector_count=len(vec_results),
            bm25_count=len(bm25_results),
            fused_count=len(fused),
            duration_ms=total,
            timings=timings,
            degraded=degraded,
        )

    # ------------------------------------------------------------------
    async def _vector_recall(
        self, kb_id: str | uuid.UUID, question: str
    ) -> list[dict[str, Any]]:
        try:
            qvec = await self.embedder.embed_query(question)
        except ModelServiceError as e:
            raise RuntimeError(f"embed_failed: {e.message}") from e
        return chroma_store.query(kb_id, qvec, top_k=settings.RETRIEVE_VECTOR_TOP_K)

    async def _bm25_recall(self, kb_id: str | uuid.UUID, question: str) -> list[Any]:
        # BM25 是同步 CPU 计算，但实测只有几毫秒，不值得丢线程池
        return self.bm25.search(kb_id, question, top_k=settings.RETRIEVE_BM25_TOP_K)

    async def _hydrate(
        self,
        fused: list[FusedHit],
        vec_results: list[dict[str, Any]],
        kb_id: str | uuid.UUID,
        user_id: str | uuid.UUID | None,
    ) -> list[Candidate]:
        """补全候选的文本与元数据。

        ## 为什么必须补 PG，不能跳过「只被 BM25 命中」的块

        第一版直接跳过了它们 —— **那等于让 BM25 失效**。

        BM25 的全部价值就在于召回「向量没找到、但关键词精确匹配」的块
        （编号 `P-001`、缩写 `OCR`、专有名词）。这些块如果在向量结果里没有，
        跳过就是白做了 BM25。

        所以这里要回 PG 查文本。检索层依赖数据库是可以接受的 ——
        它本来就在一个用户会话里运行，拿得到 `chunks` 表。
        """
        from sqlalchemy import text

        from app.db.session import get_session_factory, set_session_user

        by_id: dict[str, dict[str, Any]] = {h["id"]: h for h in vec_results}
        # **所有**候选都要补 filename：Chroma 的 metadata 里没有这个字段，
        # 只有 BM25 命中的才需要补文本，但文件名两类候选都缺。
        missing = [f.chunk_id for f in fused]

        # 缺的从 PG 补。
        #
        # **必须设 RLS 上下文** —— chunks 表受 RLS 保护，
        # 不设 app.user_id 时 `WHERE id = ANY(...)` 查不到任何行（会静默返回空，
        # 不报错）。实测踩到：BM25 明明有命中，候选却是空的。
        #
        # 这里用调用方传入的 user_id。**不要在检索层自己查 kb 归属** ——
        # `knowledge_bases` 同样受 RLS 保护，没设上下文就查不到，会陷入
        # 「要先有身份才能查身份」的死循环。
        pg_rows: dict[str, dict[str, Any]] = {}
        if missing and user_id is not None:
            factory = get_session_factory()
            async with factory() as s, s.begin():
                await set_session_user(s, user_id)
                # 顺带 JOIN 出文件名 —— Chroma 里没存 filename，
                # 导致引用只能显示「资料 · 第1页」而不是「某报告 · 第1页」。
                # 用户靠 label 找原文，缺文件名等于引用不可用。
                rows = await s.execute(
                    text(
                        "SELECT c.id, c.text, c.page_from, c.sheet_name, "
                        "       c.heading_path, c.chunk_type, c.row_range, "
                        "       c.document_id, d.filename "
                        "  FROM chunks c "
                        "  LEFT JOIN documents d ON d.id = c.document_id "
                        " WHERE c.id = ANY(:ids)"
                    ),
                    {"ids": [uuid.UUID(c) for c in missing if _is_uuid(c)]},
                )
                for m in rows.mappings().all():
                    pg_rows[str(m["id"])] = {
                        "text": m["text"],
                        "metadata": {
                            "page": m["page_from"],
                            "sheet": m["sheet_name"],
                            "heading_path": m["heading_path"],
                            "chunk_type": m["chunk_type"],
                            "row": m["row_range"],
                            "document_id": str(m["document_id"]),
                            "filename": m["filename"] or "",
                        },
                    }

        out: list[Candidate] = []
        for f in fused:
            v = by_id.get(f.chunk_id)
            if v is not None:
                meta = dict(v.get("metadata", {}))
                # 用 PG 的 filename 覆盖（Chroma 里没有）
                if f.chunk_id in pg_rows:
                    meta["filename"] = pg_rows[f.chunk_id]["metadata"].get("filename", "")
                if "document_id" not in meta or not meta.get("document_id"):
                    meta["document_id"] = pg_rows.get(f.chunk_id, {}).get(
                        "metadata", {}
                    ).get("document_id", "")
                out.append(
                    Candidate(
                        chunk_id=f.chunk_id,
                        text=v.get("text", ""),
                        rrf_score=f.rrf_score,
                        hit_count=f.hit_count,
                        sources=f.sources,
                        metadata=meta,
                    )
                )
                continue

            pg = pg_rows.get(f.chunk_id)
            if pg is None:
                # PG 里也没有 —— 可能是孤儿向量或已删除，丢弃并记日志
                log.warning("chunk_missing", chunk_id=f.chunk_id)
                continue

            out.append(
                Candidate(
                    chunk_id=f.chunk_id,
                    text=pg["text"],
                    rrf_score=f.rrf_score,
                    hit_count=f.hit_count,
                    sources=f.sources,
                    metadata=pg["metadata"],
                )
            )
        return out


def _is_uuid(s: str) -> bool:
    try:
        uuid.UUID(s)
        return True
    except (ValueError, AttributeError):
        return False


def get_retriever() -> Retriever:
    return Retriever()
