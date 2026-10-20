"""P6 真模型端到端验收。

**需要 LM Studio + PostgreSQL**。

验收的不是「能跑」，而是架构 §13.2 的几条硬指标：

1. 双路召回都返回结果
2. RRF 融合后候选数合理
3. 生成带引用且校验通过
4. 端到端延迟在预算内（首 token < 20s）
5. 多轮追问的改写生效
6. 无资料时正确拒答
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from app.core.config import settings
from app.db.repos import ChunkRepo, DocumentRepo
from app.services.qa import NO_CONTEXT_ANSWER, Answerer
from tests.p4.test_index_integration import _admin, user_tx

pytestmark = [pytest.mark.db, pytest.mark.real_model]

PUBLIC = Path(__file__).resolve().parents[3] / "eval" / "fixtures" / "public"
PDF = PUBLIC / "上海汇通能源股份有限公司2026年第三季度报告.pdf"


def _model_ok() -> bool:
    try:
        r = httpx.get(f"{settings.CHAT_BASE_URL}/models", trust_env=False, timeout=5.0)
        return r.status_code == 200 and settings.CHAT_MODEL in [
            m["id"] for m in r.json().get("data", [])
        ]
    except Exception:
        return False


pytestmark = [
    *pytestmark,
    pytest.mark.skipif(not _model_ok(), reason=f"模型 {settings.CHAT_MODEL} 不可用"),
]


@pytest.fixture
async def kb() -> AsyncIterator[dict]:
    """建一个已索引的知识库（用真 embedding）。"""
    from app.index import chroma_store
    from app.retrieval.bm25 import get_manager

    suffix = uuid.uuid4().hex[:8]
    with _admin() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (username, password_hash) VALUES (%s, 'x') RETURNING id",
            (f"qa_{suffix}",),
        )
        uid = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO knowledge_bases (user_id, name, quota_bytes) "
            "VALUES (%s, %s, %s) RETURNING id",
            (uid, f"qa_{suffix}", settings.KB_QUOTA_BYTES),
        )
        kid = cur.fetchone()[0]

    # 写入接近真实的测试资料
    from app.rag.chunker import Chunk

    texts = [
        ("一线城市（北京、上海、广州、深圳）住宿标准每晚不超过480元，超标部分由个人承担。", 1),
        ("二线城市住宿标准每晚不超过350元，其他城市每晚不超过280元。", 1),
        ("特殊情况需在出差前提交书面说明，经分管副总批准后可据实报销。", 2),
        ("员工出差期间的市内交通费，凭票据实报实销，单日上限80元。", 2),
        ("采购编号 P-001 的智能网关单价1280元，含三年质保。", 3),
        ("供应商每年复审一次，连续两年评定为B级以下的移出名录。", 4),
        ("合同用印需法务审核后由行政部统一加盖，紧急用印需总经理批准。", 5),
        ("年度预算调整需在每季度末前提交，超过50万元需董事会审批。", 6),
    ]
    chunks = [
        Chunk(text=t, seq=i, source="制度汇编.pdf", page=p)
        for i, (t, p) in enumerate(texts)
    ]

    async with user_tx(str(uid)) as s:
        doc_id = await DocumentRepo(s).create(
            kb_id=str(kid), user_id=str(uid), filename="制度汇编.pdf",
            mime="application/pdf", ext="pdf", size_bytes=2048,
            sha256=uuid.uuid4().hex * 2,
        )
        await ChunkRepo(s).bulk_insert(
            document_id=doc_id, kb_id=str(kid), user_id=str(uid), chunks=chunks
        )
        await s.execute(
            text("UPDATE documents SET status='ready', chunk_count=:n WHERE id=:d"),
            {"n": len(chunks), "d": doc_id},
        )

    await _index_vectors(str(kid), str(doc_id), texts)

    yield {"user_id": str(uid), "kb_id": str(kid), "doc_id": str(doc_id)}

    chroma_store.drop_collection(kid)
    get_manager().drop(kid)
    with _admin() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM users WHERE id = %s", (uid,))


async def _index_vectors(kb_id: str, doc_id: str, texts: list[tuple[str, int]]) -> None:
    """用真 embedding 写 Chroma + BM25。"""
    from app.index import chroma_store
    from app.rag.chunker import Chunk
    from app.rag.models import EmbeddingClient
    from app.retrieval.bm25 import get_manager

    async with user_tx(_kb_owner(kb_id)) as s:
        rows = await s.execute(
            text(
                "SELECT id, text, page_from FROM chunks "
                " WHERE document_id = :d ORDER BY seq"
            ),
            {"d": doc_id},
        )
        chunk_rows = rows.mappings().all()

    # chunk_id 必须回填成 PG 的真实 UUID，否则检索后回查不到原文
    chunks = [
        Chunk(
            text=m["text"], seq=i, chunk_id=str(m["id"]),
            source="制度汇编.pdf", page=m["page_from"],
        )
        for i, m in enumerate(chunk_rows)
    ]

    emb = EmbeddingClient()
    vectors = await emb.embed_documents([m["text"] for m in chunk_rows])
    chroma_store.add_chunks(kb_id, doc_id, chunks, vectors)

    mgr = get_manager()
    mgr.add_documents(kb_id, [(str(m["id"]), m["text"]) for m in chunk_rows])
    entry = mgr._entry(kb_id)  # noqa: SLF001
    with entry.lock:
        entry.index.rebuild(force=True)


def _kb_owner(kb_id: str) -> str:
    with _admin() as conn, conn.cursor() as cur:
        cur.execute("SELECT user_id FROM knowledge_bases WHERE id = %s", (kb_id,))
        return str(cur.fetchone()[0])


class TestSingleTurnE2E:
    async def test_factual_question_with_citation(self, kb: dict) -> None:
        """单轮事实问答：必须带引用且校验通过。"""
        answerer = Answerer()
        t0 = time.perf_counter()
        ans = await answerer.answer(
            kb["kb_id"], "一线城市住宿标准是多少？",
            user_id=kb["user_id"], turn_count=1,
        )
        elapsed = time.perf_counter() - t0

        print(f"\n[实测] 单轮问答 {elapsed:.1f}s")
        print(f"  答案：{ans.answer[:100]}")
        print(f"  引用数：{len(ans.citations)}")
        print(f"  引用校验：{ans.citation_check}")
        print(f"  耗时：{ans.timings}")

        assert ans.answer
        assert not ans.caveat, f"不该有无引用提示：{ans.caveat}"

        # 必须包含 480 这个数值（资料里的关键事实）
        assert "480" in ans.answer, f"答案未包含关键数值：{ans.answer}"

        # 必须带引用，且答案正文里要有 [n] 标注
        assert ans.citations, "没有引用"
        assert "[" in ans.answer, f"答案未带编号：{ans.answer[:80]}"
        assert ans.citation_check.get("is_valid") is True, (
            f"引用校验未通过：{ans.citation_check}"
        )

    async def test_retrieval_both_channels(self, kb: dict) -> None:
        """双路召回都应返回结果。"""
        from app.retrieval.retriever import Retriever

        r = Retriever()
        result = await r.retrieve(
            kb["kb_id"], "住宿标准", user_id=kb["user_id"], turn_count=1
        )

        print(f"\n[实测] 双路召回 向量={result.vector_count} BM25={result.bm25_count} 融合={result.fused_count}")
        assert result.vector_count > 0, "向量召回为空"
        assert result.bm25_count > 0, "BM25 召回为空"
        assert result.fused_count > 0

    async def test_keyword_precision(self, kb: dict) -> None:
        """关键词查询（P-001）应精确命中 —— 这是 BM25 的价值所在。"""
        from app.retrieval.retriever import Retriever

        r = Retriever()
        result = await r.retrieve(
            kb["kb_id"], "P-001 多少钱", user_id=kb["user_id"], turn_count=1
        )

        assert result.candidates
        # 前三条里应当出现含 P-001 的块
        top_texts = " ".join(c.text for c in result.candidates[:3])
        assert "P-001" in top_texts, f"关键词没被精确召回：{[c.text[:30] for c in result.candidates[:3]]}"

    async def test_no_context_refusal(self, kb: dict) -> None:
        """完全无关的问题必须拒答，不能编造。"""
        answerer = Answerer()
        ans = await answerer.answer(
            kb["kb_id"], "量子计算机的退相干时间是多少纳秒？",
            user_id=kb["user_id"], turn_count=1,
        )

        print(f"\n[实测] 无关问题回答：{ans.answer[:80]}")
        # 要么明确拒答，要么说明资料中未提及
        combined = ans.answer + ans.caveat
        assert (
            "未提及" in combined
            or "没有" in combined
            or ans.answer == NO_CONTEXT_ANSWER
        ), f"未正确拒答：{ans.answer}"


class TestMultiTurnRewrite:
    async def test_pronoun_followup_uses_rewrite(self, kb: dict) -> None:
        """多轮追问「那它的上限呢」应触发改写并成功检索。"""
        history = [
            {"role": "user", "content": "一线城市住宿标准是多少？"},
            {"role": "assistant", "content": "一线城市住宿标准每晚不超过480元[1]。"},
        ]

        answerer = Answerer()
        ans = await answerer.answer(
            kb["kb_id"], "那它的上限呢？",
            user_id=kb["user_id"], history=history, turn_count=2,
        )

        rw = ans.retrieval.rewrite if ans.retrieval else None
        print(f"\n[实测] 改写触发={rw.triggered if rw else None} "
              f"改写结果={rw.question if rw else None}")
        print(f"  答案：{ans.answer[:100]}")

        assert rw is not None
        assert rw.triggered is True, "第二轮指代追问未触发改写"
        # 改写应该补全了指代（结果不该还是「那它的上限呢」）
        if rw.rewritten:
            assert rw.question != "那它的上限呢？"
        assert ans.answer


class TestLatencyBudget:
    async def test_first_token_under_budget(self, kb: dict) -> None:
        """流式首 token 时间。架构预算是 20 秒。"""
        answerer = Answerer()
        t0 = time.perf_counter()
        first_token_at: float | None = None
        events = 0

        async for evt in answerer.stream_answer(
            kb["kb_id"], "供应商复审周期是多久？",
            user_id=kb["user_id"], turn_count=1,
        ):
            events += 1
            if evt["event"] == "delta" and first_token_at is None:
                first_token_at = time.perf_counter() - t0
            if evt["event"] == "done":
                break

        total = time.perf_counter() - t0
        print(f"\n[实测] 流式：首 token {first_token_at:.1f}s，总 {total:.1f}s，事件 {events}")

        assert first_token_at is not None
        assert first_token_at < 20.0, f"首 token {first_token_at:.1f}s 超过 20s 预算"
        assert events >= 2


class TestStreamEvents:
    async def test_event_sequence(self, kb: dict) -> None:
        """事件顺序必须是 meta → delta+ → done。"""
        answerer = Answerer()
        collected: list[str] = []
        done_data: dict[str, Any] = {}

        async for evt in answerer.stream_answer(
            kb["kb_id"], "市内交通费上限多少？",
            user_id=kb["user_id"], turn_count=1,
        ):
            collected.append(evt["event"])
            if evt["event"] == "done":
                done_data = evt["data"]

        assert collected[0] == "meta", f"第一个事件应为 meta，实际 {collected[0]}"
        assert "delta" in collected
        assert collected[-1] == "done"
        assert done_data.get("answer")
        assert done_data.get("citations")

    async def test_meta_has_citations_before_generation(self, kb: dict) -> None:
        """meta 事件必须带引用 —— 前端要能先渲染出处。"""
        answerer = Answerer()
        meta_citations: list = []

        async for evt in answerer.stream_answer(
            kb["kb_id"], "住宿标准",
            user_id=kb["user_id"], turn_count=1,
        ):
            if evt["event"] == "meta":
                meta_citations = evt["data"]["citations"]
                break

        assert meta_citations, "meta 事件没有引用"
        assert "index" in meta_citations[0]
        assert "label" in meta_citations[0]


class TestDegradation:
    async def test_empty_kb_no_llm_call(self, kb: dict) -> None:
        """空知识库直接拒答，不调 LLM（省时间且避免编造）。"""
        answerer = Answerer()
        ans = await answerer.answer(
            "00000000-0000-0000-0000-000000000001", "任何问题",
            user_id=kb["user_id"], turn_count=1,
        )
        assert ans.answer == NO_CONTEXT_ANSWER
        # 没有生成耗时，说明没调 LLM
        assert "generate" not in ans.timings
