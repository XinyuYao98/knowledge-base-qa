"""Excel 解析器测试。

**这是解析层最关键的一组测试。** 三个坑：

1. 合并单元格只在左上角有值
2. 双行表头压平
3. 空行分隔的多张逻辑表
"""

from __future__ import annotations

import pytest

from app.rag.parsers import BlockType, parse_file
from tests.parsers.conftest import fixture_path


class TestMergedCells:
    """合并单元格 + 双行表头。"""

    @pytest.fixture(scope="class")
    @classmethod
    def doc(cls):  # type: ignore[no-untyped-def]
        return parse_file(fixture_path("xlsx_merged_cells.xlsx"))

    def test_table_found(self, doc) -> None:  # type: ignore[no-untyped-def]
        assert doc.table_count == 1

    def test_merged_cell_expanded(self, doc) -> None:  # type: ignore[no-untyped-def]
        """A3:A4 合并，第 4 行原值为 None，展开后应为「华东」。

        这是本解析器最重要的行为：不展开的话，
        「周慕云 960 万」这一行会失去区域归属，检索时无法回答
        「华东区谁完成了多少」。
        """
        table = next(b for b in doc.blocks if b.type == BlockType.TABLE)
        lines = table.text.split("\n")
        row_zhou = next((ln for ln in lines if "周慕云" in ln), "")
        assert "华东" in row_zhou, f"合并单元格未展开：{row_zhou}"

    def test_two_row_header_flattened(self, doc) -> None:  # type: ignore[no-untyped-def]
        """双行表头应压平成「季度销售-Q1」，而不是保留两行。"""
        table = next(b for b in doc.blocks if b.type == BlockType.TABLE)
        header = table.text.split("\n")[0]
        assert "季度销售-Q1" in header
        assert "季度销售-Q2" in header
        assert table.header_rows == 2

    def test_merged_cell_warning(self, doc) -> None:  # type: ignore[no-untyped-def]
        """合并单元格必须产生告警 —— 让用户知道数据被填充过。"""
        codes = [str(w.code) for w in doc.warnings]
        assert "merged_cell" in codes

    def test_row_count(self, doc) -> None:  # type: ignore[no-untyped-def]
        table = next(b for b in doc.blocks if b.type == BlockType.TABLE)
        assert table.extra["row_count"] == 4


class TestMultiTables:
    """空行分隔的两张逻辑表。"""

    @pytest.fixture(scope="class")
    @classmethod
    def doc(cls):  # type: ignore[no-untyped-def]
        return parse_file(fixture_path("xlsx_multi_tables.xlsx"))

    def test_two_tables_detected(self, doc) -> None:  # type: ignore[no-untyped-def]
        assert doc.table_count == 2, f"应识别 2 张表，实际 {doc.table_count}"

    def test_tables_not_merged(self, doc) -> None:  # type: ignore[no-untyped-def]
        """两张表的表头不能串味。"""
        tables = [b for b in doc.blocks if b.type == BlockType.TABLE]
        t1_header = tables[0].text.split("\n")[0]
        t2_header = tables[1].text.split("\n")[0]
        assert "产品编号" in t1_header
        assert "园区" in t2_header
        assert "园区" not in t1_header
        assert "产品编号" not in t2_header

    def test_single_row_header(self, doc) -> None:  # type: ignore[no-untyped-def]
        """普通单行表头不能被误判成双行。"""
        tables = [b for b in doc.blocks if b.type == BlockType.TABLE]
        assert all(t.header_rows == 1 for t in tables)

    def test_data_preserved(self, doc) -> None:  # type: ignore[no-untyped-def]
        tables = [b for b in doc.blocks if b.type == BlockType.TABLE]
        assert "P-001" in tables[0].text
        assert "苏州园区" in tables[1].text


class TestCellFormatting:
    @pytest.fixture(scope="class")
    @classmethod
    def doc(cls):  # type: ignore[no-untyped-def]
        return parse_file(fixture_path("xlsx_merged_cells.xlsx"))

    def test_integer_float_cleanup(self, doc) -> None:  # type: ignore[no-untyped-def]
        """1280.0 应输出 1280，不是 1280.0 —— 否则检索时数字匹配会失败。"""
        text = next(b for b in doc.blocks if b.type == BlockType.TABLE).text
        assert "1280.5" in text  # 小数保留
        assert "960.0" not in text  # 整数去掉 .0
        assert "960" in text

    def test_sheet_name_recorded(self, doc) -> None:  # type: ignore[no-untyped-def]
        table = next(b for b in doc.blocks if b.type == BlockType.TABLE)
        assert table.sheet == "区域销售"

    def test_col_span(self, doc) -> None:  # type: ignore[no-untyped-def]
        table = next(b for b in doc.blocks if b.type == BlockType.TABLE)
        assert table.col_span == "A:E"
