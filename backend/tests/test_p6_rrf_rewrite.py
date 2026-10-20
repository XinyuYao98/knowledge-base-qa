"""RRF 融合与查询改写测试。

**不需要数据库、不需要模型服务**。
"""

from __future__ import annotations

import pytest

from app.retrieval.rewriter import (
    PRONOUN_PATTERN,
    RewriteResult,
    _clean,
    _format_history,
    should_rewrite,
)
from app.retrieval.rrf import RRF_K, explain, fuse


class TestRRF:
    def test_single_channel(self) -> None:
        """单路时，融合分数就是原始排名的倒数贡献。"""
        fused = fuse({"vec": [("a", 0.9), ("b", 0.8)]})
        assert [h.chunk_id for h in fused] == ["a", "b"]
        assert fused[0].rrf_score == pytest.approx(1 / (RRF_K + 1))

    def test_both_channels_hit_wins(self) -> None:
        """两路都命中的，应该排在只被一路命中的前面。

        这是 RRF 的核心价值：多路共识 > 单路高分。
        """
        fused = fuse(
            {
                "vec": [("only_vec", 0.95), ("both", 0.80)],
                "bm25": [("both", 12.0), ("only_bm25", 8.0)],
            }
        )
        assert fused[0].chunk_id == "both"
        assert fused[0].hit_count == 2

    def test_score_is_sum_of_reciprocals(self) -> None:
        fused = fuse({"vec": [("a", 0.9)], "bm25": [("a", 10.0)]})
        expected = 1 / (RRF_K + 1) + 1 / (RRF_K + 1)
        assert fused[0].rrf_score == pytest.approx(expected)

    def test_raw_scores_ignored(self) -> None:
        """RRF 只看排名，不看原始分数。

        BM25 的 12.0 和向量的 0.95 不可比，融合时不该被数值大小影响。
        """
        a = fuse({"x": [("p", 0.99), ("q", 0.01)]})
        b = fuse({"x": [("p", 9999.0), ("q", 1.0)]})
        assert [h.chunk_id for h in a] == [h.chunk_id for h in b]
        assert a[0].rrf_score == pytest.approx(b[0].rrf_score)

    def test_top_rank_beats_lower_rank(self) -> None:
        fused = fuse({"x": [("first", 0.1), ("second", 0.9)]})
        # 排名优先，不因原始分数低就排后面
        assert fused[0].chunk_id == "first"

    def test_limit(self) -> None:
        items = [(f"c{i}", 1.0) for i in range(30)]
        fused = fuse({"x": items}, limit=5)
        assert len(fused) == 5

    def test_empty_inputs(self) -> None:
        assert fuse({}) == []
        assert fuse({"vec": [], "bm25": []}) == []

    def test_sources_recorded(self) -> None:
        """要能查到「这条为什么排第一」—— 排查依赖这个。"""
        fused = fuse({"vec": [("a", 0.9)], "bm25": [("a", 5.0)]})
        src = fused[0].sources
        assert src["vec"]["rank"] == 1
        assert src["bm25"]["rank"] == 1
        assert src["vec"]["score"] == 0.9

    def test_stable_for_ties(self) -> None:
        """同分时顺序应稳定（不因字典顺序随机变化）。"""
        r1 = fuse({"x": [("a", 1.0), ("b", 1.0)]})
        r2 = fuse({"x": [("a", 1.0), ("b", 1.0)]})
        assert [h.chunk_id for h in r1] == [h.chunk_id for h in r2]

    def test_explain_readable(self) -> None:
        text = explain({"vec": [("abc123def456", 0.9)], "bm25": [("abc123def456", 5.0)]})
        assert "[vec]" in text
        assert "[融合后 Top3]" in text


class TestPronounDetection:
    @pytest.mark.parametrize(
        "text",
        [
            "那它的上限呢？",
            "这个怎么算？",
            "上述规定适用吗？",
            "前者和后者有什么区别？",
            "刚才说的那个呢？",
        ],
    )
    def test_pronoun_hit(self, text: str) -> None:
        assert PRONOUN_PATTERN.search(text), f"应识别为指代：{text}"

    @pytest.mark.parametrize(
        "text",
        [
            "一线城市住宿标准是多少？",
            "采购审批权限是什么？",
            "出差补贴每天多少钱？",
        ],
    )
    def test_no_pronoun(self, text: str) -> None:
        assert not PRONOUN_PATTERN.search(text), f"不该误判：{text}"


class TestShouldRewrite:
    def test_first_turn_never_rewrites(self) -> None:
        """首轮没有历史可指代，即使用户写了「这个」也不改。"""
        assert should_rewrite("这个怎么用？", turn_count=1) is False

    def test_pronoun_after_first_turn(self) -> None:
        assert should_rewrite("那它的上限呢？", turn_count=2) is True

    def test_complete_question_second_turn(self) -> None:
        """第二轮的完整问句不该触发改写 —— 触发率约 30% 的意义就在这里。"""
        assert should_rewrite("采购审批权限是什么？", turn_count=2) is False


class TestClean:
    def test_strip_prefix(self) -> None:
        assert _clean("改写后：一线城市住宿标准上限") == "一线城市住宿标准上限"

    def test_strip_quotes(self) -> None:
        assert _clean("「一线城市住宿标准上限」") == "一线城市住宿标准上限"
        assert _clean('"标准上限"') == "标准上限"

    def test_strip_whitespace(self) -> None:
        assert _clean("  问题  ") == "问题"

    def test_no_change_needed(self) -> None:
        assert _clean("一线城市住宿标准") == "一线城市住宿标准"


class TestFormatHistory:
    def test_empty(self) -> None:
        assert _format_history([]) == "（无）"

    def test_roles(self) -> None:
        h = [
            {"role": "user", "content": "问题一"},
            {"role": "assistant", "content": "答案一"},
        ]
        text = _format_history(h)
        assert "用户：问题一" in text
        assert "助手：答案一" in text

    def test_truncates_long_content(self) -> None:
        """单条过长要截断 —— 否则改写器的上下文会被一段长回答淹没。"""
        h = [{"role": "assistant", "content": "x" * 500}]
        text = _format_history(h)
        assert "…" in text
        assert len(text) < 300

    def test_limits_recent_turns(self) -> None:
        h = []
        for i in range(20):
            h.append({"role": "user", "content": f"问{i}"})
            h.append({"role": "assistant", "content": f"答{i}"})
        text = _format_history(h, max_turns=2)
        assert "问19" in text
        assert "问0" not in text


class TestRewriteResult:
    def test_to_dict_has_prompt_version(self) -> None:
        """prompt_version 必须进结果 —— 出问题时要能定位是哪一版提示词。"""
        r = RewriteResult(question="q", original="q", rewritten=False, triggered=False)
        d = r.to_dict()
        assert d["prompt_version"]
        assert d["degraded"] == ""
        assert d["triggered"] is False
