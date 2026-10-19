"""引用提取与校验测试。

这个模块是「答案可追溯」的核心，必须能脱离数据库与模型单独验证。
"""

from __future__ import annotations

import pytest

from app.rag.citations import (
    check_citations,
    extract_citations,
    format_context,
    is_refusal,
    strip_citations,
)


class TestExtract:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("结论 [1]", [1]),
            ("结论 [1][2]", [1, 2]),
            ("结论 [1] 与 [3]", [1, 3]),
            ("无引用的一句话", []),
        ],
    )
    def test_single_and_multiple(self, text: str, expected: list[int]) -> None:
        assert [c.index for c in extract_citations(text)] == expected

    def test_range_notation(self) -> None:
        """模型实测会写 `[1-5]` 这种区间，必须能解析。

        第一次跑竖切片时就是因为不支持区间，误判为「引用无效」。
        """
        assert [c.index for c in extract_citations("未提及 [1-5]")] == [1, 2, 3, 4, 5]

    def test_comma_notation(self) -> None:
        assert [c.index for c in extract_citations("见 [1,2,3]")] == [1, 2, 3]

    def test_chinese_comma(self) -> None:
        assert [c.index for c in extract_citations("见 [1，2]")] == [1, 2]

    def test_reversed_range_normalized(self) -> None:
        assert [c.index for c in extract_citations("[5-3]")] == [3, 4, 5]

    def test_huge_range_truncated(self) -> None:
        """防止 [1-999] 生成巨量编号拖垮内存。"""
        cites = extract_citations("[1-999]")
        assert len(cites) <= 60

    def test_out_of_sane_range_ignored(self) -> None:
        """[999] 超过合理上限，直接忽略而不是当成有效引用。"""
        assert extract_citations("见 [999]") == []

    def test_full_width_brackets_not_matched(self) -> None:
        """全角括号不认 —— 只在明确约定方括号时才提取，避免误判。"""
        assert extract_citations("见 【1】") == []


class TestPosition:
    def test_start_end(self) -> None:
        text = "结论 [12] 结尾"
        c = extract_citations(text)[0]
        assert text[c.start : c.end] == "[12]"

    def test_range_shares_position(self) -> None:
        text = "见 [1-3]"
        cites = extract_citations(text)
        assert len({(c.start, c.end) for c in cites}) == 1


class TestCheck:
    def test_valid_single(self) -> None:
        r = check_citations("答案 [1]", available=5)
        assert r.is_valid
        assert r.cited == [1]
        assert r.invalid == []

    def test_invalid_out_of_range(self) -> None:
        """引用了不存在的来源编号 → 不通过。"""
        r = check_citations("答案 [7]", available=5)
        assert not r.is_valid
        assert r.invalid == [7]

    def test_no_citation_not_refusal(self) -> None:
        """有结论但没标注来源 → 不通过。"""
        r = check_citations("这是个没有来源的结论。", available=5)
        assert not r.is_valid

    def test_refusal_without_citation_passes(self) -> None:
        """拒答时不需要引用 —— 没有结论可归属，硬标编号反而是编造。"""
        r = check_citations("根据资料，未提及此事。", available=5)
        assert r.is_valid
        assert r.cited == []

    def test_unused_computed(self) -> None:
        r = check_citations("答案 [1][3]", available=5)
        assert r.unused == [2, 4, 5]

    def test_empty_text(self) -> None:
        r = check_citations("", available=3)
        assert not r.is_valid


class TestRefusal:
    @pytest.mark.parametrize(
        "text",
        ["资料中未提及", "未提及相关信息", "资料中没有这条", "无法从资料判断"],
    )
    def test_markers(self, text: str) -> None:
        assert is_refusal(text)

    def test_normal_answer_not_refusal(self) -> None:
        assert not is_refusal("一线城市每晚不超过 480 元 [1]")


class TestStrip:
    def test_remove_citations(self) -> None:
        assert strip_citations("答案 [1] 与 [2]") == "答案  与"


class TestFormatContext:
    def test_numbering(self) -> None:
        items = [
            {"text": "第一条内容", "label": "制度 第1页"},
            {"text": "第二条内容", "label": "制度 第2页"},
        ]
        ctx, used = format_context(items)
        assert "[1] 制度 第1页" in ctx
        assert "[2] 制度 第2页" in ctx
        assert len(used) == 2
        assert used[0]["citation_index"] == 1

    def test_truncate_by_whole_item(self) -> None:
        """超长时整条丢弃，不切半条 —— 切半条会产生断句，模型会读错。"""
        items = [
            {"text": "A" * 100, "label": "1"},
            {"text": "B" * 100, "label": "2"},
            {"text": "C" * 100, "label": "3"},
        ]
        ctx, used = format_context(items, max_chars=250)
        assert len(used) == 2
        assert "C" * 10 not in ctx

    def test_skip_empty(self) -> None:
        items = [{"text": "", "label": "空"}, {"text": "有内容", "label": "有"}]
        ctx, used = format_context(items)
        assert len(used) == 1
