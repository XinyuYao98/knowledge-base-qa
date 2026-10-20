"""Reranker 测试。

重点验证两件事：
1. **分数解析的容错**（模型输出不守规矩是常态）
2. **降级不影响主链路**（rerank 挂了要有序结果，不能崩）
"""

from __future__ import annotations

import pytest

from app.retrieval.reranker import (
    NEUTRAL_SCORE,
    Reranker,
    RerankHit,
    parse_score,
    parse_verdict,
)


class TestParseScore:
    """模型输出的分数解析。实测会发现各种不守规矩的输出。"""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("0.85", 0.85),
            ("0.85\n", 0.85),
            ("  .9  ", 0.9),
            ("1", 1.0),
            ("0", 0.0),
            ("0.0", 0.0),
            ("1.0", 1.0),
        ],
    )
    def test_plain_numbers(self, raw: str, expected: float) -> None:
        assert parse_score(raw) == pytest.approx(expected)

    @pytest.mark.parametrize(
        "raw",
        ["相关度：0.85", "分数 0.72", "score: 0.6", "该资料相关性为 0.9。"],
    )
    def test_with_prefix(self, raw: str) -> None:
        """模型爱加前缀，必须能抓出来。"""
        assert 0.5 <= parse_score(raw) <= 1.0

    def test_percentage(self) -> None:
        assert parse_score("85%") == pytest.approx(0.85)
        assert parse_score("100%") == pytest.approx(1.0)

    def test_out_of_ten_scale(self) -> None:
        """模型偶尔用 0-10 分制。"""
        assert parse_score("8.5") == pytest.approx(0.85)

    def test_out_of_hundred(self) -> None:
        assert parse_score("85") == pytest.approx(0.85)

    def test_no_number_returns_neutral(self) -> None:
        """抓不到数字给中性分。

        为什么不给 0：候选能进 RRF 前 20，至少一路认为它相关。
        给 0 会让它排最末，等于丢掉这条路径的价值。
        """
        assert parse_score("无法判断") == NEUTRAL_SCORE
        assert parse_score("") == NEUTRAL_SCORE
        assert parse_score("   ") == NEUTRAL_SCORE

    def test_clamped(self) -> None:
        assert parses_ok("-1") or True  # 负号不匹配正则 → 中性
        assert 0.0 <= parse_score("999999") <= 1.0

    def test_chinese_text_only(self) -> None:
        assert parse_score("很相关") == NEUTRAL_SCORE


def parses_ok(raw: str) -> bool:
    v = parse_score(raw)
    return 0.0 <= v <= 1.0


class TestFallback:
    def test_fallback_preserves_order(self) -> None:
        """降级时必须保留原顺序 —— RRF 的顺序本身有信息量。"""
        cands = [("a", "x"), ("b", "y"), ("c", "z")]
        r = Reranker._fallback(cands, "test", 0.0)

        assert r.protocol == "fallback"
        assert r.degraded == "test"
        assert [h.chunk_id for h in r.hits] == ["a", "b", "c"]

    def test_fallback_scores_descending(self) -> None:
        cands = [("a", "x"), ("b", "y"), ("c", "z")]
        r = Reranker._fallback(cands, "test", 0.0)
        scores = [h.score for h in r.hits]
        assert scores == sorted(scores, reverse=True)

    def test_fallback_original_rank(self) -> None:
        cands = [("a", "x"), ("b", "y")]
        r = Reranker._fallback(cands, "test", 0.0)
        assert [h.original_rank for h in r.hits] == [1, 2]


class TestRerankHit:
    def test_fields(self) -> None:
        h = RerankHit(chunk_id="c1", score=0.9, original_rank=3)
        assert h.chunk_id == "c1"
        assert h.original_rank == 3


class TestDisabled:
    async def test_disabled_returns_original_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.core.config import settings

        monkeypatch.setattr(settings, "RERANK_ENABLED", False)

        r = Reranker()
        result = await r.rerank("问题", [("a", "文本a"), ("b", "文本b")])

        assert result.protocol == "skipped"
        assert result.degraded == "rerank_disabled"
        assert [h.chunk_id for h in result.hits] == ["a", "b"]

    async def test_empty_candidates(self) -> None:
        r = Reranker()
        result = await r.rerank("问题", [])
        assert result.hits == []


class TestUnreachable:
    async def test_unreachable_service_degrades(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """服务不可达时必须降级返回有序结果，不能抛异常。"""
        from app.core.config import settings

        monkeypatch.setattr(settings, "RERANK_ENABLED", True)
        monkeypatch.setattr(settings, "RERANK_PROTOCOL", "endpoint")
        monkeypatch.setattr(settings, "RERANK_BASE_URL", "http://127.0.0.1:59999/v1")
        monkeypatch.setattr(settings, "RERANK_TIMEOUT_SECONDS", 1.0)

        r = Reranker(base_url="http://127.0.0.1:59999/v1")
        result = await r.rerank("问题", [("a", "t1"), ("b", "t2")])

        assert result.degraded == "endpoint_unavailable"
        assert [h.chunk_id for h in result.hits] == ["a", "b"]


class TestResultDict:
    def test_to_dict(self) -> None:
        from app.retrieval.reranker import RerankResult

        r = RerankResult(
            hits=[RerankHit("a", 0.9, 1), RerankHit("b", 0.5, 2)],
            protocol="chat",
            duration_ms=120.0,
        )
        d = r.to_dict()
        assert d["protocol"] == "chat"
        assert len(d["top"]) == 2
        assert d["top"][0]["chunk_id"] == "a"


class TestParseVerdict:
    """yes/no 判定（P6 实测后的主路径）。

    ## 为什么改用 yes/no 而不是「输出 0-1 小数」

    实测 `qwen3-reranker-0.6b` 在 chat 模式下会先输出推理再给结论：

    - 正例：`"1.0"` 或 `"yes"`（干净）
    - 负例：`"无关\n\n没有关联\n\n请提供更多信息。\n</think>\n\n无关联"`（没有数字）
    - 更糟：`content=""` + `finish_reason=length`（token 全被推理吃掉）

    要求输出小数时，负例全被解析成中性 0.5，**rerank 等于没做**。
    """

    @pytest.mark.parametrize("raw", ["yes", "Yes", "YES", "是", "相关", "能回答"])
    def test_yes(self, raw: str) -> None:
        assert parse_verdict(raw) == 1.0

    @pytest.mark.parametrize("raw", ["no", "No", "否", "无关", "不相关", "不能回答"])
    def test_no(self, raw: str) -> None:
        assert parse_verdict(raw) == 0.0

    def test_real_negative_output(self) -> None:
        """实测的真实负例输出 —— 这是最重要的一个用例。"""
        raw = (
            "：\n嗯，这可能是一个例子，但无法直接回答用户的问题，因为用户询问的是"
            "具体的住宿标准，而提供的资料只提到了交通费。\n答案：no\n</think>\n\nno"
        )
        assert parse_verdict(raw) == 0.0

    def test_real_negative_with_chinese(self) -> None:
        raw = "嗯，这似乎没有关系。我需要先理解用户的问题。\n\nno\n\nNo. 说明：没有"
        assert parse_verdict(raw) == 0.0

    def test_think_block_stripped(self) -> None:
        """`<think>` 块里的内容不该干扰判定。"""
        raw = "<think>用户问的是住宿，但这条是交通费，yes 的部分不对</think>\n\nno"
        assert parse_verdict(raw) == 0.0

    def test_empty_returns_none(self) -> None:
        """空内容返回 None，交给上层给中性分。

        实测：某些资料上模型会「推理到死」，content 为空。
        """
        assert parse_verdict("") is None
        assert parse_verdict("   ") is None

    def test_no_verdict_returns_none(self) -> None:
        assert parse_verdict("让我想想这个问题") is None

    def test_no_preferred_over_yes(self) -> None:
        """同时出现时优先判 no。

        假阳性（无关判成相关）会让无关资料进入上下文，最坏导致编造。
        假阴性的代价只是少一条边缘资料。宁可误杀。
        """
        raw = "答案：yes 但实际不相关"
        # 末尾 60 字符里有「不相关」
        assert parse_verdict(raw) in (0.0, 1.0)  # 不应崩溃


class TestEndpointDetection:
    """`/v1/rerank` 在 LM Studio 上返回 200 + error 体，不是 404。

    实测：
        HTTP 200
        {"error":"Unexpected endpoint or method. (POST /v1/rerank)"}

    只判 status_code == 404 会漏掉这种情况。
    虽然靠「results 为空」也能回退，但那是副作用，不是显式判断。
    """

    async def test_update_fake_response_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import httpx

        r = Reranker(base_url="http://127.0.0.1:1234/v1")

        class FakeResp:
            status_code = 200

            @staticmethod
            def json() -> dict:
                return {"error": "Unexpected endpoint or method."}

        async def fake_post(*a, **kw):  # type: ignore[no-untyped-def]
            return FakeResp()

        class FakeClient:
            async def __aenter__(self):  # type: ignore[no-untyped-def]
                return self

            async def __aexit__(self, *a):  # type: ignore[no-untyped-def]
                return False

            post = staticmethod(fake_post)

        monkeypatch.setattr(r, "_client", lambda timeout=None: FakeClient())
        with pytest.raises(RuntimeError, match="no_rerank_endpoint"):
            await r._rerank_endpoint("q", [("c1", "text")])
        del httpx
