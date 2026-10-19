"""索引数据的持久化层。

## 为什么单独一层

worker 与 API 都要写 documents / chunks / ingest_jobs，
如果不封装，两边会各自拼 SQL，迟早不一致。

这里的每个方法都在**调用方的事务**里执行，不自己开事务 ——
这样「写文档 + 写块 + 更新状态」可以原子完成。

## RLS 注意

这些是后台任务（worker），不是用户请求。worker 用系统会话，
但**必须显式带上 user_id**（表里是 NOT NULL），不要依赖 RLS 兜底。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger

log = get_logger("repo")

#: 文档状态机。
#:
#:   pending → parsing → chunking → embedding → ready
#:                          ↓
#:                       failed
#:
#: `waiting_model` 是特殊态：模型服务不可用，任务留在队列里等，不算失败。
#:
#: ## 重跑路径（实测踩到）
#:
#: `ready → parsing` 必须允许：用户在界面上点「重新索引」时要能从 ready 开始。
#: 第一版漏了这一条，重跑直接被状态机拒绝（TransitionError）。
#:
#: 同理 `parsing/chunking/embedding → pending`：
#: 用户改了切块参数想从头再来，不能强制他先走完当前流程。
DOC_STATUS_FLOW: dict[str, tuple[str, ...]] = {
    "pending": ("parsing", "failed"),
    "parsing": ("chunking", "failed", "pending"),
    "chunking": ("embedding", "failed", "pending"),
    "embedding": ("ready", "failed", "waiting_model", "pending"),
    "waiting_model": ("embedding", "failed", "pending"),
    "ready": ("parsing", "failed", "pending"),   # 允许重跑
    "failed": ("pending", "parsing"),            # 允许直接重试
}


class TransitionError(Exception):
    """非法的状态迁移。"""


def _to_uuid(v: str | uuid.UUID) -> uuid.UUID:
    return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))


class DocumentRepo:
    """documents 表操作。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        kb_id: str | uuid.UUID,
        user_id: str | uuid.UUID,
        filename: str,
        mime: str,
        ext: str,
        size_bytes: int,
        sha256: str,
        storage_key: str = "",
    ) -> uuid.UUID:
        row = await self.session.execute(
            text(
                """
                INSERT INTO documents
                    (kb_id, user_id, filename, mime, ext, size_bytes, sha256, storage_key)
                VALUES
                    (:kb_id, :user_id, :filename, :mime, :ext, :size, :sha, :key)
                RETURNING id
                """
            ),
            {
                "kb_id": _to_uuid(kb_id),
                "user_id": _to_uuid(user_id),
                "filename": filename,
                "mime": mime,
                "ext": ext,
                "size": size_bytes,
                "sha": sha256,
                "key": storage_key,
            },
        )
        return row.scalar_one()

    async def get(self, doc_id: str | uuid.UUID) -> dict[str, Any] | None:
        row = await self.session.execute(
            text("SELECT * FROM documents WHERE id = :id"), {"id": _to_uuid(doc_id)}
        )
        m = row.mappings().first()
        return dict(m) if m else None

    async def find_by_sha(
        self, kb_id: str | uuid.UUID, sha256: str
    ) -> dict[str, Any] | None:
        """秒传：同库同内容直接复用。"""
        row = await self.session.execute(
            text("SELECT * FROM documents WHERE kb_id = :kb AND sha256 = :sha"),
            {"kb": _to_uuid(kb_id), "sha": sha256},
        )
        m = row.mappings().first()
        return dict(m) if m else None

    async def update_status(
        self,
        doc_id: str | uuid.UUID,
        new_status: str,
        *,
        error_code: str | None = None,
        error_detail: str | None = None,
        enforce_flow: bool = True,
    ) -> None:
        """更新状态。默认校验迁移合法性 —— 非法迁移会让人以为流程错了。"""
        if enforce_flow:
            cur = await self.session.execute(
                text("SELECT status FROM documents WHERE id = :id FOR UPDATE"),
                {"id": _to_uuid(doc_id)},
            )
            current = cur.scalar_one_or_none()
            if current is None:
                raise TransitionError(f"文档不存在：{doc_id}")
            allowed = DOC_STATUS_FLOW.get(current, ())
            if new_status not in allowed:
                raise TransitionError(
                    f"非法状态迁移：{current} → {new_status}（允许：{allowed}）"
                )

        await self.session.execute(
            text(
                """
                UPDATE documents
                   SET status = :st,
                       error_code = :ec,
                       error_detail = :ed
                 WHERE id = :id
                """
            ),
            {"st": new_status, "ec": error_code, "ed": error_detail, "id": _to_uuid(doc_id)},
        )

    async def set_parse_meta(self, doc_id: str | uuid.UUID, meta: dict[str, Any]) -> None:
        import json

        await self.session.execute(
            text("UPDATE documents SET parse_meta = CAST(:m AS jsonb) WHERE id = :id"),
            {"m": json.dumps(meta, ensure_ascii=False), "id": _to_uuid(doc_id)},
        )

    async def set_ready(
        self,
        doc_id: str | uuid.UUID,
        *,
        chunk_count: int,
        embed_model: str,
        parse_meta: dict[str, Any] | None = None,
    ) -> None:
        import json

        await self.session.execute(
            text(
                """
                UPDATE documents
                   SET status = 'ready',
                       chunk_count = :n,
                       embed_model = :em,
                       error_code = NULL,
                       error_detail = NULL,
                       parse_meta = CAST(:pm AS jsonb)
                 WHERE id = :id
                """
            ),
            {
                "n": chunk_count,
                "em": embed_model,
                "pm": json.dumps(parse_meta or {}, ensure_ascii=False),
                "id": _to_uuid(doc_id),
            },
        )


class ChunkRepo:
    """chunks 表操作。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def delete_by_document(self, doc_id: str | uuid.UUID) -> int:
        r = await self.session.execute(
            text("DELETE FROM chunks WHERE document_id = :id"), {"id": _to_uuid(doc_id)}
        )
        return r.rowcount or 0

    async def bulk_insert(
        self,
        *,
        document_id: str | uuid.UUID,
        kb_id: str | uuid.UUID,
        user_id: str | uuid.UUID,
        chunks: list[Any],
    ) -> int:
        """批量写入切块。返回写入条数。

        用 executemany 而不是逐条 INSERT：403 块逐条往返要 400+ 次网络 IO。
        """
        if not chunks:
            return 0

        rows = [
            {
                "document_id": _to_uuid(document_id),
                "kb_id": _to_uuid(kb_id),
                "user_id": _to_uuid(user_id),
                "level": "child",
                "seq": c.seq,
                "chunk_type": c.chunk_type,
                "heading_path": c.heading_path or "",
                "page_from": c.page,
                "page_to": c.page,
                "sheet_name": c.sheet,
                "row_range": str(c.row) if c.row is not None else None,
                "text": c.text,
                "token_count": c.token_estimate,
            }
            for c in chunks
        ]

        await self.session.execute(
            text(
                """
                INSERT INTO chunks
                    (document_id, kb_id, user_id, level, seq, chunk_type,
                     heading_path, page_from, page_to, sheet_name, row_range,
                     text, token_count)
                VALUES
                    (:document_id, :kb_id, :user_id, :level, :seq, :chunk_type,
                     :heading_path, :page_from, :page_to, :sheet_name, :row_range,
                     :text, :token_count)
                """
            ),
            rows,
        )
        return len(rows)

    async def count(self, doc_id: str | uuid.UUID) -> int:
        r = await self.session.execute(
            text("SELECT count(*) FROM chunks WHERE document_id = :id"),
            {"id": _to_uuid(doc_id)},
        )
        return int(r.scalar_one())


class JobRepo:
    """ingest_jobs 表操作。

    ## 队列消费的关键：SKIP LOCKED

    多个 worker 同时轮询时，`FOR UPDATE SKIP LOCKED` 保证每个任务只被一个
    worker 拿到。没有它，两个 worker 会同时处理同一文档，向量重复写入。
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def enqueue(
        self, *, document_id: str | uuid.UUID, user_id: str | uuid.UUID
    ) -> uuid.UUID:
        row = await self.session.execute(
            text(
                """
                INSERT INTO ingest_jobs (document_id, user_id)
                VALUES (:doc, :user)
                RETURNING id
                """
            ),
            {"doc": _to_uuid(document_id), "user": _to_uuid(user_id)},
        )
        return row.scalar_one()

    async def claim_one(self) -> dict[str, Any] | None:
        """原子地领一个待处理任务。没有任务时返回 None。"""
        row = await self.session.execute(
            text(
                """
                UPDATE ingest_jobs
                   SET status = 'running',
                       attempts = attempts + 1,
                       updated_at = now()
                 WHERE id = (
                       SELECT id FROM ingest_jobs
                        WHERE status IN ('queued', 'retrying', 'waiting_model')
                          AND run_after <= now()
                        ORDER BY created_at
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                 )
                RETURNING id, document_id, user_id, attempts, stage
                """
            )
        )
        m = row.mappings().first()
        return dict(m) if m else None

    async def finish(self, job_id: str | uuid.UUID, *, stage: str = "done") -> None:
        await self.session.execute(
            text(
                """
                UPDATE ingest_jobs
                   SET status = 'done', stage = :st, last_error = NULL, updated_at = now()
                 WHERE id = :id
                """
            ),
            {"st": stage, "id": _to_uuid(job_id)},
        )

    async def retry_later(
        self,
        job_id: str | uuid.UUID,
        *,
        error: str,
        delay_seconds: int,
        waiting_model: bool = False,
    ) -> None:
        """安排重试。模型不可用时用 waiting_model，不算失败。"""
        await self.session.execute(
            text(
                """
                UPDATE ingest_jobs
                   SET status = :st,
                       last_error = :err,
                       run_after = now() + make_interval(secs => :delay),
                       updated_at = now()
                 WHERE id = :id
                """
            ),
            {
                "st": "waiting_model" if waiting_model else "retrying",
                "err": error[:2000],
                "delay": delay_seconds,
                "id": _to_uuid(job_id),
            },
        )

    async def fail(self, job_id: str | uuid.UUID, *, error: str) -> None:
        await self.session.execute(
            text(
                """
                UPDATE ingest_jobs
                   SET status = 'failed', last_error = :err, updated_at = now()
                 WHERE id = :id
                """
            ),
            {"err": error[:2000], "id": _to_uuid(job_id)},
        )

    async def stats(self) -> dict[str, int]:
        row = await self.session.execute(
            text("SELECT status, count(*) AS n FROM ingest_jobs GROUP BY status")
        )
        return {r[0]: int(r[1]) for r in row.fetchall()}
