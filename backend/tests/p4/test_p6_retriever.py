"""检索编排测试（连真库，用假 embedder）。

验证的重点不是「能检索」，而是几个**行为契约**：

1. 只被 BM25 命中的块 **必须保留**（否则 BM25 白做）
2. RRF 让两路共识的结果排前面
3. 单路失败时降级而非崩溃
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.db.repos import ChunkRepo, DocumentRepo
from app.retrieval.retriever import Candidate, Retriever
from tests._fake_embedder import FakeEmbedder
from tests.p4.test_index_integration import _admin, user_tx

pytestmark = pytest.mark.db


@pytest.fixture
async def kb() -> AsyncIterator[dict]:
    suffix = uuid.uuid4().hex[:8]
    with _admin() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (username, password_hash) VALUES (%s, 'x') RETURNING id",
            (f"rt_{suffix}",),
        )
        uid = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO knowledge_bases (user_id, name, quota_bytes) "
            "VALUES (%s, %s, %s) RETURNING id",
            (uid, f"rt_{suffix}", settings.KB_QUOTA_BYTES),
        )
        kid = cur.fetchone()[0]

    yield {"user_id": str(uid), "kb_id": str(kid)}

    from app.index import chroma_store
    from app.retrieval.bm25 import get_manager

    chroma_store.drop_collection(kid)
    get_manager().drop(kid)
    with _admin() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM users WHERE id = %s", (uid,))


CHUNKS = [
    ("一线城市住宿标准每晚不超过480元，超标部分由个人承担。", {"page": 1}),
    ("二线城市住宿标准每晚不超过350元。", {"page": 1}),
    ("采购编号 P-001 智能网关单价1280元。", {"page": 2}),
    ("供应商每年复审一次，连续两年B级以下移出名录。", {"page": 3}),
    ("合同用印需法务审核后由行政部统一加盖。", {"page": 4}),
]


async def _seed(kb: dict) -> list[str]:
    """写入 chunks 到 PG + BM25，返回 chunk_id 列表。"""
    from app.rag.chunker import Chunk
    from app.retrieval.bm25 import get_manager

    chunk_objs = [
        Chunk(text=text_, seq=i, source="测试.txt", page=meta.get("page"))
        for i, (text_, meta) in enumerate(CHUNKS)
    ]

    async with user_tx(kb["user_id"]) as s:
        doc_id = await DocumentRepo(s).create(
            kb_id=kb["kb_id"], user_id=kb["user_id"], filename="测试.txt",
            mime="text/plain", ext="txt", size_bytes=1,
            sha256=uuid.uuid4().hex * 2,
        )
        await ChunkRepo(s).bulk_insert(
            document_id=doc_id, kb_id=kb["kb_id"],
            user_id=kb["user_id"], chunks=chunk_objs,
        )

    async with user_tx(kb["user_id"]) as s:
        rows = await s.execute(
            text("SELECT id, text FROM chunks ORDER BY seq")
        )
        items = [(str(m["id"]), m["text"]) for m in rows.mappings().all()]

    mgr = get_manager()
    mgr.add_documents(kb["kb_id"], items)
    # 强制重建，确保索引可用
    entry = mgr._entry(kb["kb_id"])  # noqa: SLF001
    with entry.lock:
        entry.index.rebuild(force=True)

    return [cid for cid, _ in items]


class TestBm25OnlyHitsPreserved:
    async def test_bm25_only_chunks_are_kept(self, kb: dict) -> None:
        """**核心契约**：只在 BM25 里命中的块，不能因为向量没召回就丢掉。

        BM25 的价值就是召回「向量找不到但关键词精确匹配」的块。
        如果这里丢掉，BM25 等于白做。
        """
        await _seed(kb)

        r = Retriever(embedder=FakeEmbedder(settings.EMBED_DIM))
        # 用一个向量检索不到（collection 为空）但 BM25 能命中的查询
        result = await r.retrieve(kb["kb_id"], user_id=kb["user_id"], question="P-001", turn_count=1)

        # curl 的 FakeEmbedder 无法生成有意义的向量，
        # 所以这里主要是验证：当向量为空时，BM25 结果能被补全
        assert result.bm25_count > 0, "BM25 应该有命中"
        assert result.candidates, (
            "向量为空时，BM25 命中的块必须被保留并补全"
        )
        # 被保留的块必须有文本（从 PG 补的）
        assert all(c.text for c in result.candidates)

    async def test_metadata_hydrated_from_pg(self, kb: dict) -> None:
        """从 PG 补出的候选必须带元数据（页码等），引用定位依赖它。"""
        await _seed(kb)
        r = Retriever(embedder=FakeEmbedder(settings.EMBED_DIM))
        result = await r.retrieve(kb["kb_id"], user_id=kb["user_id"], question="采购编号", turn_count=1)

        assert result.candidates
        c = result.candidates[0]
        assert c.metadata, "元数据不能为空"
        assert "page" in c.metadata


class TestDegradation:
    async def test_vector_unavailable_still_returns_bm25(self, kb: dict) -> None:
        """向量服务挂了，BM25 结果仍要返回。"""
        await _seed(kb)

        class BrokenEmbedder:
            async def embed_documents(self, texts: list[str]) -> list[list[float]]:
                raise RuntimeError("down")

            async def embed_query(self, q: str) -> list[float]:
                raise RuntimeError("down")

        r = Retriever(embedder=BrokenEmbedder())  # type: ignore[arg-type]
        result = await r.retrieve(kb["kb_id"], user_id=kb["user_id"], question="住宿标准", turn_count=1)

        assert "vector_unavailable" in result.degraded
        assert result.bm25_count > 0
        assert result.candidates, "向量挂了也要靠 BM25 出结果"

    async def test_empty_kb_returns_empty(self, kb: dict) -> None:
        r = Retriever(embedder=FakeEmbedder(settings.EMBED_DIM))
        result = await r.retrieve(kb["kb_id"], user_id=kb["user_id"], question="任何问题", turn_count=1)
        assert result.empty
        assert result.duration_ms >= 0


class TestRewriteIntegration:
    async def test_first_turn_no_rewrite(self, kb: dict) -> None:
        """首轮不触发改写，question 应与原问题一致。"""
        r = Retriever(embedder=FakeEmbedder(settings.EMBED_DIM))
        result = await r.retrieve(kb["kb_id"], user_id=kb["user_id"], question="这个怎么算？", turn_count=1)

        assert result.rewrite is not None
        assert result.rewrite.triggered is False
        assert result.question == "这个怎么算？"

    async def test_second_turn_triggers_but_degrades_without_model(
        self, kb: dict
    ) -> None:
        """第二轮命中指代词会触发改写；模型不可用时降级但不影响检索。"""
        r = Retriever(embedder=FakeEmbedder(settings.EMBED_DIM))
        result = await r.retrieve(kb["kb_id"], user_id=kb["user_id"], question="那它的上限呢？",
            history=[{"role": "user", "content": "住宿标准是多少"}],
            turn_count=2,
        )

        assert result.rewrite is not None
        assert result.rewrite.triggered is True
        # 模型没开 → 降级，但仍用原问题检索
        if result.rewrite.degraded:
            assert "no_rewrite" in result.degraded
        assert result.question


class TestRRFOrdering:
    async def test_candidate_has_rrf_score(self, kb: dict) -> None:
        await _seed(kb)
        r = Retriever(embedder=FakeEmbedder(settings.EMBED_DIM))
        result = await r.retrieve(kb["kb_id"], user_id=kb["user_id"], question="住宿标准", turn_count=1)

        if result.candidates:
            scores = [c.rrf_score for c in result.candidates]
            assert scores == sorted(scores, reverse=True), "候选必须按 RRF 降序"
            assert all(c.rrf_score > 0 for c in result.candidates)


class TestTimings:
    async def test_timings_recorded(self, kb: dict) -> None:
        """分阶段耗时必须记录 —— 排查慢在哪一步要用。"""
        r = Retriever(embedder=FakeEmbedder(settings.EMBED_DIM))
        result = await r.retrieve(kb["kb_id"], user_id=kb["user_id"], question="测试", turn_count=1)

        assert "rewrite" in result.timings
        assert "recall" in result.timings
        assert "fuse" in result.timings
        assert result.duration_ms >= 0


class TestCandidate:
    def test_to_dict_truncates_text(self) -> None:
        c = Candidate(
            chunk_id="c1", text="x" * 500, rrf_score=0.5, hit_count=2,
        )
        d = c.to_dict()
        assert len(d["text"]) <= 120
