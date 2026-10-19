"""真模型端到端验收（需要 LM Studio 运行）。

这是 P4 的**最终验收**：用真实 embedding 服务把文件索引到 ready。

## 与 test_index_integration.py 的区别

那份用 FakeEmbedder，验证的是**逻辑**（落库、状态机、幂等）。
这份用真模型，验证的是**真实写入**：维度对不对、Chroma 收不收、
真实耗时多少、真实块数多少。

**两者都不能省**：逻辑测试能在任何机器跑；真模型测试证明「真的能用」。

## 跳过条件

模型服务不可达时自动跳过（不是 fail）—— 否则 CI 环境永远红。
但本地跑 P4 验收时必须看到它通过，不能当它不存在。
"""

from __future__ import annotations

import shutil
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

from app.core.config import settings
from app.db.repos import ChunkRepo, DocumentRepo, JobRepo
from app.services.indexer import Indexer
from app.workers.stages import stage_dir
from tests.p4.test_index_integration import user_tx  # noqa: F401

pytestmark = [pytest.mark.db, pytest.mark.real_model]

PUBLIC = Path(__file__).resolve().parents[3] / "eval" / "fixtures" / "public"
PDF = PUBLIC / "上海汇通能源股份有限公司2026年第三季度报告.pdf"
HTML = PUBLIC / "2025年全国科技经费投入统计公报 - 国家统计局.html"


def _model_available() -> bool:
    try:
        r = httpx.get(
            f"{settings.EMBED_BASE_URL}/models",
            trust_env=False,
            timeout=5.0,
        )
        if r.status_code != 200:
            return False
        ids = [m["id"] for m in r.json().get("data", [])]
        return settings.EMBED_MODEL in ids
    except Exception:
        return False


pytestmark = [
    *pytestmark,
    pytest.mark.skipif(
        not _model_available(),
        reason=f"模型服务不可用或模型 {settings.EMBED_MODEL} 未加载",
    ),
]


def _admin():
    return psycopg.connect(settings.DATABASE_ADMIN_URL, autocommit=True, connect_timeout=5)


@pytest.fixture
async def ctx() -> Any:
    suffix = uuid.uuid4().hex[:8]
    with _admin() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (username, password_hash) VALUES (%s, 'x') RETURNING id",
            (f"rm_{suffix}",),
        )
        uid = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO knowledge_bases (user_id, name, quota_bytes) "
            "VALUES (%s, %s, %s) RETURNING id",
            (uid, f"rm_{suffix}", settings.KB_QUOTA_BYTES),
        )
        kid = cur.fetchone()[0]

    yield {"user_id": str(uid), "kb_id": str(kid)}

    from app.index import chroma_store

    chroma_store.drop_collection(kid)
    with _admin() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM users WHERE id = %s", (uid,))


async def _index(ctx: dict, src: Path) -> Any:
    """把文件跑完索引，返回 IndexOutcome。"""
    async with user_tx(ctx["user_id"]) as s:
        doc_id = await DocumentRepo(s).create(
            kb_id=ctx["kb_id"],
            user_id=ctx["user_id"],
            filename=src.name,
            mime="application/octet-stream",
            ext=src.suffix.lstrip("."),
            size_bytes=src.stat().st_size,
            sha256=uuid.uuid4().hex * 2,
            storage_key=str(src),
        )

    try:
        async with user_tx(ctx["user_id"]) as s:
            out = await Indexer(s).index(doc_id, src)
        return doc_id, out
    finally:
        pass


class TestRealModelIndex:
    async def test_pdf_to_ready(self, ctx: dict) -> None:
        """真实 PDF：12 页、13 张表、403 块（含 379 表格行块）。"""
        from app.index import chroma_store

        t0 = time.perf_counter()
        doc_id, out = await _index(ctx, PDF)
        elapsed = time.perf_counter() - t0

        try:
            assert out.ok, f"索引失败：{out.error_code} — {out.error_detail}"
            assert out.status == "ready"

            # 与 P3 管道阶段的实测值对齐
            assert out.chunk_count >= 380, f"块数偏少：{out.chunk_count}"
            assert out.vector_count == out.chunk_count

            async with user_tx(ctx["user_id"]) as s:
                doc = await DocumentRepo(s).get(doc_id)
                n = await ChunkRepo(s).count(doc_id)

            assert doc["status"] == "ready"
            assert doc["chunk_count"] == n == out.chunk_count
            assert doc["embed_model"] == settings.EMBED_MODEL

            # Chroma 里真的有这么多条
            stats = chroma_store.stats(ctx["kb_id"])
            assert stats.get("count") == out.chunk_count, (
                f"Chroma 有 {stats.get('count')} 条，PG 有 {out.chunk_count} 条"
            )

            # 真实耗时（PDF 403 块）
            print(f"\n[实测] PDF 索引总耗时 {elapsed:.1f}s，{out.chunk_count} 块")
            for k, v in out.stages.items():
                print(f"       {k}: {v:.0f}ms")
            print(f"       健康分：{out.warnings or '无问题'}")
        finally:
            shutil.rmtree(stage_dir(str(doc_id)).path, ignore_errors=True)

    async def test_html_to_ready(self, ctx: dict) -> None:
        """真实 HTML：6 张表、约 200 块。"""
        from app.index import chroma_store

        doc_id, out = await _index(ctx, HTML)
        try:
            assert out.ok, f"索引失败：{out.error_code} — {out.error_detail}"
            assert out.chunk_count >= 150
            assert out.vector_count == out.chunk_count
            assert chroma_store.stats(ctx["kb_id"]).get("count") == out.chunk_count
            print(f"\n[实测] HTML 索引 {out.chunk_count} 块")
        finally:
            shutil.rmtree(stage_dir(str(doc_id)).path, ignore_errors=True)

    async def test_vector_dimension_matches_config(self, ctx: dict) -> None:
        """真实向量维度必须等于配置值 —— 不符会让 Chroma 拒绝或检索错乱。"""
        from app.index import chroma_store
        from app.rag.models import EmbeddingClient

        emb = EmbeddingClient()
        vec = await emb.embed_query("一线城市住宿标准")
        assert len(vec) == settings.EMBED_DIM, (
            f"配置 {settings.EMBED_DIM}，实际 {len(vec)}"
        )
        del chroma_store

    async def test_reindex_real_model(self, ctx: dict) -> None:
        """真实重索引：块数稳定、不重复。"""
        doc_id, out1 = await _index(ctx, PDF)
        try:
            async with user_tx(ctx["user_id"]) as s:
                out2 = await Indexer(s).index(doc_id, PDF)

            assert out2.ok
            assert out2.chunk_count == out1.chunk_count, (
                f"重跑块数变了：{out1.chunk_count} → {out2.chunk_count}"
            )

            async with user_tx(ctx["user_id"]) as s:
                n = await ChunkRepo(s).count(doc_id)
            assert n == out2.chunk_count
        finally:
            shutil.rmtree(stage_dir(str(doc_id)).path, ignore_errors=True)


class TestWorkerRealModel:
    async def test_worker_processes_with_real_embedding(self, ctx: dict) -> None:
        """worker 用真模型消费队列，跑到 ready。"""
        from app.workers import ingest_worker

        async with user_tx(ctx["user_id"]) as s:
            doc_id = await DocumentRepo(s).create(
                kb_id=ctx["kb_id"], user_id=ctx["user_id"], filename=PDF.name,
                mime="application/pdf", ext="pdf", size_bytes=PDF.stat().st_size,
                sha256=uuid.uuid4().hex * 2, storage_key=str(PDF),
            )
            await JobRepo(s).enqueue(document_id=doc_id, user_id=ctx["user_id"])

        try:
            await ingest_worker.Worker(once=True, concurrency=1).run()

            async with user_tx(ctx["user_id"]) as s:
                doc = await DocumentRepo(s).get(doc_id)
                js = await JobRepo(s).stats()

            assert doc["status"] == "ready", (
                f"状态 {doc['status']}，错误 {doc['error_code']}：{doc['error_detail']}"
            )
            assert doc["chunk_count"] >= 380
            assert js.get("done", 0) >= 1
            print(f"\n[实测] worker 真实索引完成：{doc['chunk_count']} 块")
        finally:
            shutil.rmtree(stage_dir(str(doc_id)).path, ignore_errors=True)
