"""索引编排：管道产物 → PG + Chroma。

## 为什么单独一层

worker 只管「领任务、调 indexer、收尾」，不关心怎么落库。
这样 indexer 可以被 CLI、worker、测试三种入口复用。

## 一致性策略：先 PG 后 Chroma

顺序很重要：

1. 先写 chunks 到 PG（事务内）
2. 再写 vectors 到 Chroma

**为什么这个顺序**：如果先写 Chroma 后写 PG，一旦 PG 失败，
会出现「Chroma 有向量但 PG 没有 chunk」的孤儿向量 —— 检索命中后取不到原文。
反过来，PG 有了但 Chroma 没写，最坏情况是「检索不到」，可以靠重跑修复。

**孤儿向量的兜底**：`make reindex` 全量重建（架构 §6.3 已写）。

## 重复索引的处理

重跑同一文档必须先删旧数据，否则：
- PG 里 chunks 重复（seq 冲突但不唯一，会静默堆积）
- Chroma 的 id 是 `{doc_id}_{seq}`，upsert 会覆盖，但 PG 会变两份

所以每次索引前 `DELETE FROM chunks WHERE document_id = ...`。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.repos import ChunkRepo, DocumentRepo
from app.index import chroma_store
from app.services import quality
from app.workers.pipeline import Pipeline, PipelineError
from app.workers.stages import stage_dir

log = get_logger("indexer")


@dataclass(slots=True)
class IndexOutcome:
    document_id: str
    status: str
    chunk_count: int = 0
    vector_count: int = 0
    duration_ms: float = 0.0
    stages: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error_code: str | None = None
    error_detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ready"

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "status": self.status,
            "chunk_count": self.chunk_count,
            "vector_count": self.vector_count,
            "duration_ms": round(self.duration_ms, 1),
            "stages": {k: round(v, 1) for k, v in self.stages.items()},
            "warnings": self.warnings,
            "error_code": self.error_code,
            "error_detail": self.error_detail,
        }


class Indexer:
    """把一个文档从源文件索引到 ready。"""

    def __init__(self, session: AsyncSession, pipeline: Pipeline | None = None) -> None:
        self.session = session
        self.pipeline = pipeline or Pipeline()
        self.docs = DocumentRepo(session)
        self.chunks = ChunkRepo(session)

    async def index(
        self,
        doc_id: str | uuid.UUID,
        source: Path,
        *,
        force_stage: str | None = None,
    ) -> IndexOutcome:
        """完整索引。失败时返回带 error_code 的结果，不抛异常。

        不抛异常的原因：worker 需要把错误码写进 documents，
        抛异常会让调用方拿不到结构化信息。
        """
        from app.workers.stages import Stage

        doc_id_s = str(doc_id)
        started = time.perf_counter()
        outcome = IndexOutcome(document_id=doc_id_s, status="failed")

        # ---- 状态推进 ----
        # 允许从任意状态重跑：先打回 pending，再走正常流程。
        # 否则 ready 状态的文档点「重新索引」会被状态机拒绝。
        current = (await self.docs.get(doc_id_s) or {}).get("status")
        if current not in (None, "pending"):
            await self.docs.update_status(doc_id_s, "pending")
        await self.docs.update_status(doc_id_s, "parsing")

        # ---- 跑管道 ----
        start_stage = Stage(force_stage) if force_stage else Stage.PARSE
        try:
            pipe_out = await self.pipeline.run_from(doc_id_s, source, start_stage, reuse=True)
        except PipelineError as e:
            await self.docs.update_status(
                doc_id_s, "failed", error_code=str(e.stage), error_detail=e.message
            )
            outcome.error_code = str(e.stage)
            outcome.error_detail = e.message
            outcome.duration_ms = (time.perf_counter() - started) * 1000
            return outcome

        chunks = pipe_out.chunks
        vectors = pipe_out.vectors
        outcome.stages = dict(pipe_out.durations_ms)

        # ---- chunking → embedding ----
        await self.docs.update_status(doc_id_s, "chunking")
        await self.docs.update_status(doc_id_s, "embedding")

        # ---- 落 PG（先） ----
        doc = await self.docs.get(doc_id_s)
        if doc is None:
            outcome.error_code = "not_found"
            outcome.error_detail = f"文档不存在：{doc_id_s}"
            return outcome

        kb_id = doc["kb_id"]
        user_id = doc["user_id"]

        deleted = await self.chunks.delete_by_document(doc_id_s)
        if deleted:
            log.info("chunks_replaced", doc_id=doc_id_s, deleted=deleted)

        n_chunks = await self.chunks.bulk_insert(
            document_id=doc_id_s,
            kb_id=kb_id,
            user_id=user_id,
            chunks=chunks,
        )
        outcome.chunk_count = n_chunks

        # ---- 写 Chroma（后） ----
        if chunks and vectors:
            try:
                chroma_store.add_chunks(kb_id, doc_id_s, chunks, vectors)
                outcome.vector_count = len(vectors)
            except Exception as e:
                # Chroma 失败不回滚 PG：PG 有数据只影响检索，可重跑修复
                log.exception("chroma_write_failed", doc_id=doc_id_s)
                outcome.error_code = "chroma_write_failed"
                outcome.error_detail = f"{type(e).__name__}: {e}"
                await self.docs.update_status(
                    doc_id_s, "failed", error_code="chroma_write_failed", error_detail=str(e)[:500]
                )
                outcome.duration_ms = (time.perf_counter() - started) * 1000
                return outcome

        # ---- 质量报告 ----
        report_path = quality.write_report(
            doc_id_s, doc.get("filename", ""), doc.get("ext", "")
        )
        report = quality.build_report(doc_id_s, doc.get("filename", ""), doc.get("ext", ""))
        outcome.warnings = report.get("health", {}).get("issues", [])

        meta = stage_dir(doc_id_s).read_meta()
        await self.docs.set_ready(
            doc_id_s,
            chunk_count=n_chunks,
            embed_model=settings.EMBED_MODEL,
            parse_meta={
                "stages": meta.get("stages", {}),
                "health": report.get("health", {}),
                "report_path": str(report_path.name),
            },
        )

        outcome.status = "ready"
        outcome.duration_ms = (time.perf_counter() - started) * 1000
        log.info(
            "index_done",
            doc_id=doc_id_s,
            chunks=n_chunks,
            vectors=outcome.vector_count,
            ms=round(outcome.duration_ms, 1),
        )
        return outcome
