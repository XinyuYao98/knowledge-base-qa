"""P4 集成测试：仓储层 + 落库 + Chroma + Worker。

**需要真实 PostgreSQL**（`pytest -m db`），**不需要模型服务**（注入 FakeEmbedder）。

## 为什么必须连真库

P4 的核心是「字段对得上、约束不冲突、RLS 生效」——
用 mock 测等于测 mock 写得对不对。P3 已经踩过（健康分读错字段，
测试全绿但功能是坏的）。

## RLS 与测试

应用角色受 RLS 保护，连 `users` 表都插不进去。所以：

- **准备数据**（建用户/知识库）→ 管理员同步连接
- **测业务逻辑**（写 documents/chunks）→ 异步会话 + `set_session_user`

后者是 worker 的真实用法：worker 代表某个用户写数据，必须显式声明身份。
"""

from __future__ import annotations

import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest
from sqlalchemy import text

from app.core.config import settings
from app.db.repos import ChunkRepo, DocumentRepo, JobRepo, TransitionError
from app.db.session import get_session_factory, set_session_user
from app.services.indexer import Indexer
from app.workers.pipeline import Pipeline
from app.workers.stages import stage_dir
from tests._fake_embedder import FakeEmbedder

pytestmark = pytest.mark.db

FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "eval" / "fixtures" / "synthetic" / "txt_gbk_chinese.txt"
)


def _admin():
    """管理员同步连接，仅用于准备/清理测试数据。"""
    return psycopg.connect(settings.DATABASE_ADMIN_URL, autocommit=True, connect_timeout=5)


@asynccontextmanager
async def user_tx(user_id: str):
    """带 RLS 上下文的异步会话 —— worker 的真实用法。"""
    factory = get_session_factory()
    async with factory() as s, s.begin():
        await set_session_user(s, user_id)
        yield s


@pytest.fixture
async def ctx() -> Any:
    """隔离的测试用户 + 知识库。用完级联删除。"""
    suffix = uuid.uuid4().hex[:8]
    with _admin() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (username, password_hash) VALUES (%s, 'x') RETURNING id",
            (f"p4_{suffix}",),
        )
        uid = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO knowledge_bases (user_id, name, quota_bytes) "
            "VALUES (%s, %s, %s) RETURNING id",
            (uid, f"kb_{suffix}", settings.KB_QUOTA_BYTES),
        )
        kid = cur.fetchone()[0]

    yield {"user_id": str(uid), "kb_id": str(kid)}

    from app.index import chroma_store

    chroma_store.drop_collection(kid)
    with _admin() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM users WHERE id = %s", (uid,))


async def _new_doc(ctx: dict, filename: str = "a.txt", ext: str = "txt", key: str = "") -> uuid.UUID:
    """在 ctx 的知识库下建一个文档。"""
    async with user_tx(ctx["user_id"]) as s:
        return await DocumentRepo(s).create(
            kb_id=ctx["kb_id"],
            user_id=ctx["user_id"],
            filename=filename,
            mime="text/plain",
            ext=ext,
            size_bytes=1,
            sha256=uuid.uuid4().hex + uuid.uuid4().hex,
            storage_key=key,
        )


# ---------------------------------------------------------------------------
# 仓储层
# ---------------------------------------------------------------------------


class TestDocumentRepo:
    async def test_create_and_get(self, ctx: dict) -> None:
        doc_id = await _new_doc(ctx)
        async with user_tx(ctx["user_id"]) as s:
            doc = await DocumentRepo(s).get(doc_id)

        assert doc is not None
        assert doc["filename"] == "a.txt"
        assert doc["status"] == "pending"
        assert doc["chunk_count"] == 0

    async def test_sha_dedup(self, ctx: dict) -> None:
        sha = uuid.uuid4().hex + uuid.uuid4().hex
        async with user_tx(ctx["user_id"]) as s:
            repo = DocumentRepo(s)
            await repo.create(
                kb_id=ctx["kb_id"], user_id=ctx["user_id"], filename="a.txt",
                mime="text/plain", ext="txt", size_bytes=1, sha256=sha,
            )
            found = await repo.find_by_sha(ctx["kb_id"], sha)
            assert found is not None and found["filename"] == "a.txt"

            # 唯一约束冲突（psycopg 的 UniqueViolation 经 SQLAlchemy 包装）
            from sqlalchemy.exc import IntegrityError

            with pytest.raises(IntegrityError):
                await repo.create(
                    kb_id=ctx["kb_id"], user_id=ctx["user_id"], filename="b.txt",
                    mime="text/plain", ext="txt", size_bytes=1, sha256=sha,
                )

    async def test_rls_blocks_cross_user_write(self, ctx: dict) -> None:
        """不设置 RLS 上下文就写不进去 —— 隔离在守着。"""
        factory = get_session_factory()
        async with factory() as s, s.begin():
            with pytest.raises(Exception, match="row-level security"):
                await DocumentRepo(s).create(
                    kb_id=ctx["kb_id"], user_id=ctx["user_id"], filename="x.txt",
                    mime="text/plain", ext="txt", size_bytes=1,
                    sha256=uuid.uuid4().hex * 2,
                )


class TestStatusTransitions:
    async def test_legal_transition(self, ctx: dict) -> None:
        doc_id = await _new_doc(ctx)
        async with user_tx(ctx["user_id"]) as s:
            await DocumentRepo(s).update_status(doc_id, "parsing")
        async with user_tx(ctx["user_id"]) as s:
            doc = await DocumentRepo(s).get(doc_id)
        assert doc["status"] == "parsing"

    async def test_illegal_transition_rejected(self, ctx: dict) -> None:
        doc_id = await _new_doc(ctx)
        async with user_tx(ctx["user_id"]) as s:
            with pytest.raises(TransitionError):
                await DocumentRepo(s).update_status(doc_id, "ready")


class TestChunkRepo:
    async def test_bulk_insert_and_delete(self, ctx: dict) -> None:
        from app.rag.chunker import Chunk

        doc_id = await _new_doc(ctx)
        chunks = [
            Chunk(text=f"第{i}段正文内容，长度足够用于测试。", seq=i, page=i + 1)
            for i in range(5)
        ]

        async with user_tx(ctx["user_id"]) as s:
            n = await ChunkRepo(s).bulk_insert(
                document_id=doc_id, kb_id=ctx["kb_id"],
                user_id=ctx["user_id"], chunks=chunks,
            )
            assert n == 5
            assert await ChunkRepo(s).count(doc_id) == 5

        async with user_tx(ctx["user_id"]) as s:
            deleted = await ChunkRepo(s).delete_by_document(doc_id)
            assert deleted == 5
            assert await ChunkRepo(s).count(doc_id) == 0

    async def test_field_mapping(self, ctx: dict) -> None:
        """Chunk 字段到表字段的映射必须对 —— 引用定位靠这些。"""
        from app.rag.chunker import Chunk

        doc_id = await _new_doc(ctx, "a.xlsx", "xlsx")
        async with user_tx(ctx["user_id"]) as s:
            await ChunkRepo(s).bulk_insert(
                document_id=doc_id, kb_id=ctx["kb_id"], user_id=ctx["user_id"],
                chunks=[
                    Chunk(text="表格行内容足够长用于测试", seq=0, page=3, row=12,
                          chunk_type="table_row", sheet="汇总", heading_path="第一章"),
                ],
            )
            r = await s.execute(
                text(
                    "SELECT page_from, page_to, sheet_name, row_range, "
                    "       chunk_type, heading_path, token_count "
                    "  FROM chunks WHERE document_id = :d"
                ),
                {"d": doc_id},
            )
            row = r.mappings().one()

        assert row["page_from"] == 3
        assert row["page_to"] == 3
        assert row["sheet_name"] == "汇总"
        assert row["row_range"] == "12"
        assert row["chunk_type"] == "table_row"
        assert row["heading_path"] == "第一章"
        assert row["token_count"] > 0


class TestJobRepo:
    async def test_enqueue_and_claim(self, ctx: dict) -> None:
        doc_id = await _new_doc(ctx)
        async with user_tx(ctx["user_id"]) as s:
            job_id = await JobRepo(s).enqueue(document_id=doc_id, user_id=ctx["user_id"])

        async with user_tx(ctx["user_id"]) as s:
            job = await JobRepo(s).claim_one()

        assert job is not None
        assert str(job["id"]) == str(job_id)
        assert job["attempts"] == 1

    async def test_skip_locked_prevents_double_claim(self, ctx: dict) -> None:
        """两个 worker 不能拿到同一个任务 —— 否则向量重复写。"""
        doc_id = await _new_doc(ctx)
        async with user_tx(ctx["user_id"]) as s:
            await JobRepo(s).enqueue(document_id=doc_id, user_id=ctx["user_id"])

        async def claim():
            async with user_tx(ctx["user_id"]) as s2:
                return await JobRepo(s2).claim_one()

        r1 = await claim()
        r2 = await claim()

        assert r1 is not None
        assert r2 is None or str(r2["id"]) != str(r1["id"])

    async def test_waiting_model_keeps_attempts(self, ctx: dict) -> None:
        """模型不可用是「等待」不是「失败」—— attempts 不该增加。"""
        doc_id = await _new_doc(ctx)
        async with user_tx(ctx["user_id"]) as s:
            job_id = await JobRepo(s).enqueue(document_id=doc_id, user_id=ctx["user_id"])

        async with user_tx(ctx["user_id"]) as s:
            await JobRepo(s).retry_later(
                job_id, error="模型不可用", delay_seconds=30, waiting_model=True
            )

        async with user_tx(ctx["user_id"]) as s:
            r = await s.execute(
                text("SELECT status, attempts, last_error FROM ingest_jobs WHERE id = :i"),
                {"i": job_id},
            )
            row = r.mappings().one()

        assert row["status"] == "waiting_model"
        assert row["attempts"] == 0
        assert "模型不可用" in row["last_error"]


# ---------------------------------------------------------------------------
# 端到端索引
# ---------------------------------------------------------------------------


class TestFullIndex:
    async def test_index_to_ready(self, ctx: dict) -> None:
        from app.index import chroma_store

        doc_id = await _new_doc(
            ctx, FIXTURE.name, "txt", key=str(FIXTURE)
        )

        pipe = Pipeline(embedder=FakeEmbedder(settings.EMBED_DIM))
        try:
            async with user_tx(ctx["user_id"]) as s:
                out = await Indexer(s, pipeline=pipe).index(doc_id, FIXTURE)

            assert out.ok, f"索引失败：{out.error_code} {out.error_detail}"
            assert out.chunk_count > 0
            assert out.vector_count == out.chunk_count

            async with user_tx(ctx["user_id"]) as s:
                doc = await DocumentRepo(s).get(doc_id)
                n = await ChunkRepo(s).count(doc_id)

            assert doc["status"] == "ready"
            assert doc["chunk_count"] == n == out.chunk_count
            assert doc["embed_model"] == settings.EMBED_MODEL
            assert "health" in (doc["parse_meta"] or {})

            stats = chroma_store.stats(ctx["kb_id"])
            assert stats.get("count") == out.chunk_count
        finally:
            shutil.rmtree(stage_dir(str(doc_id)).path, ignore_errors=True)

    async def test_reindex_replaces_not_duplicates(self, ctx: dict) -> None:
        doc_id = await _new_doc(ctx, FIXTURE.name, "txt", key=str(FIXTURE))
        pipe = Pipeline(embedder=FakeEmbedder(settings.EMBED_DIM))
        try:
            out = None
            for _ in range(2):
                async with user_tx(ctx["user_id"]) as s:
                    out = await Indexer(s, pipeline=pipe).index(doc_id, FIXTURE)
                assert out is not None and out.ok

            async with user_tx(ctx["user_id"]) as s:
                n = await ChunkRepo(s).count(doc_id)

            assert n == out.chunk_count, f"重跑后应 {out.chunk_count} 块，实际 {n}"
        finally:
            shutil.rmtree(stage_dir(str(doc_id)).path, ignore_errors=True)


class TestWorker:
    async def test_worker_once_processes_queue(
        self, ctx: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """worker --once 能把队列里的任务跑到 ready。"""
        from app.workers import ingest_worker

        orig_init = Pipeline.__init__

        def patched(self, embedder=None):  # type: ignore[no-untyped-def]
            orig_init(self, embedder or FakeEmbedder(settings.EMBED_DIM))

        monkeypatch.setattr(Pipeline, "__init__", patched)

        doc_id = await _new_doc(ctx, FIXTURE.name, "txt", key=str(FIXTURE))
        async with user_tx(ctx["user_id"]) as s:
            await JobRepo(s).enqueue(document_id=doc_id, user_id=ctx["user_id"])

        try:
            await ingest_worker.Worker(once=True, concurrency=1).run()

            async with user_tx(ctx["user_id"]) as s:
                doc = await DocumentRepo(s).get(doc_id)
                js = await JobRepo(s).stats()

            assert doc["status"] == "ready", (
                f"状态 {doc['status']}，错误 {doc['error_code']}：{doc['error_detail']}"
            )
            assert js.get("done", 0) >= 1
        finally:
            shutil.rmtree(stage_dir(str(doc_id)).path, ignore_errors=True)

    async def test_missing_source_marks_failed(self, ctx: dict, monkeypatch: pytest.MonkeyPatch) -> None:
        """源文件丢失要明确失败，不能静默跳过。"""
        from app.workers import ingest_worker

        doc_id = await _new_doc(ctx, "ghost.txt", "txt", key="not/exist.txt")
        async with user_tx(ctx["user_id"]) as s:
            await JobRepo(s).enqueue(document_id=doc_id, user_id=ctx["user_id"])

        await ingest_worker.Worker(once=True, concurrency=1).run()

        async with user_tx(ctx["user_id"]) as s:
            doc = await DocumentRepo(s).get(doc_id)

        assert doc["status"] == "failed"
        assert doc["error_code"] == "source_missing"
