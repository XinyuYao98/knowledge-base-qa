"""P8：表格切块的回归测试。

## 背景

P7 评测发现表格题大量抖动。追下去是**两个不同的问题**，P8 分开处理：

| 问题 | 该不该修 | 修法 |
|---|---|---|
| 文本切分把项目名切断 | **该修** | 用 `cells` 重建，不用文本分行 |
| 表格内容重复（正文 + 表格各一份） | **该修** | 解析时按 bbox 排除表格区域 |
| 按行切得太碎 | **不该修** | 保持「一张表一个块」 |

第三条是 P8 最重要的教训：把 46 行的行业表切成 46 个块后，
评测从 78.9% 掉到 74.3% —— 单行块之间高度相似，向量 top20 几乎随机，
而页眉因为含公司名反而排第一。**答案在库里，却挤不进 5 条候选。**

所以本文件的契约是：
- 一张表（或子表）→ **一个块**
- 块内用 `列：...` 声明列名，行用 `a | b | c`
- 项目名完整（不因 PDF 换行被切断）
- 空值写「（空）」
- 表格内容不在正文块里重复
"""

from __future__ import annotations

import pytest

from app.rag.chunker import _norm_cell, chunk_document
from app.rag.parsers.base import Block, BlockType, ParsedDoc, TableCell


def _table_block(
    rows: list[list[str | None]],
    header_rows: int = 1,
    table_index: int = 1,
) -> Block:
    """构造一个带 cells 的表格块，模拟解析器输出。"""
    cells: list[TableCell] = []
    for r, row in enumerate(rows):
        for c, val in enumerate(row):
            if val is None:
                continue
            cells.append(
                TableCell(text=val, row=r, col=c, is_header=r < header_rows)
            )
    text = "\n".join(" | ".join(v or "" for v in row) for row in rows)
    return Block(
        type=BlockType.TABLE,
        text=text,
        page=1,
        table_index=table_index,
        cells=cells,
        header_rows=header_rows,
    )


def _doc(blocks: list[Block]) -> ParsedDoc:
    d = ParsedDoc(filename="test.pdf", format="pdf")
    d.blocks = blocks
    return d


class TestCellNormalization:
    """单元格归一化 —— PDF 的换行和全角空格是必须处理的。"""

    def test_removes_newlines_in_project_name(self) -> None:
        raw = "归属于上市公司股东的扣除非\n经常性损益的净利润"
        assert _norm_cell(raw) == "归属于上市公司股东的扣除非经常性损益的净利润"

    def test_removes_ideographic_space(self) -> None:
        # 统计公报里「地　区」「合　计」用全角空格对齐
        assert _norm_cell("地\u3000区") == "地区"

    def test_collapses_multi_space(self) -> None:
        assert _norm_cell("R&D  经费   （亿元）") == "R&D 经费 （亿元）"

    def test_empty(self) -> None:
        assert _norm_cell("") == ""
        assert _norm_cell("   ") == ""


class TestOneChunkPerTable:
    """一张表一个块 —— 这是 P8 的核心决定。"""

    def test_whole_table_in_one_chunk(self) -> None:
        block = _table_block(
            [
                ["项目", "本报告期", "年初至报告期末"],
                ["营业收入", "30,776,711.66", "113,977,728.63"],
                ["利润总额", "12,663,394.77", "52,553,974.94"],
                ["净利润", "9,342,183.69", "35,127,666.84"],
            ]
        )
        chunks = chunk_document(_doc([block]))
        assert len(chunks) == 1, "整张表应该是一个块，不能按行拆"

        text = chunks[0].text
        # 列名显式声明
        assert text.startswith("列：项目 / 本报告期 / 年初至报告期末")
        # 三行数据都在
        assert "营业收入 | 30,776,711.66 | 113,977,728.63" in text
        assert "利润总额 | 12,663,394.77 | 52,553,974.94" in text
        assert "净利润 | 9,342,183.69 | 35,127,666.84" in text
        assert chunks[0].extra["row_count"] == 3

    def test_long_table_split_with_header_repeated(self) -> None:
        """超长表才切分，且**每块都带列名声明**。

        切分在这里是必需的（单块塞不下），不是设计选择。
        """
        rows: list[list[str | None]] = [["项目", "金额"]]
        for i in range(600):
            rows.append([f"项目{i}", str(i * 100)])

        chunks = chunk_document(_doc([_table_block(rows)]))
        assert len(chunks) > 1, "超长表应当被切分"
        for c in chunks:
            assert c.text.startswith("列：项目 / 金额"), "切分后每块都要带列名"


class TestNoTruncation:
    """项目名不能因 PDF 换行被切断 —— P7 最典型的缺陷。"""

    def test_project_name_with_newline(self) -> None:
        block = _table_block(
            [
                ["非经常性损益项目", "本期金额", "年初至报告期末金额", "说明"],
                [
                    "非流动性资产处置损益，包括已计提资产减值准备\n的冲销部分",
                    None,
                    "616,631.23",
                    None,
                ],
            ]
        )
        text = chunk_document(_doc([block]))[0].text

        # 项目名必须完整
        assert "非流动性资产处置损益，包括已计提资产减值准备的冲销部分" in text
        # 旧实现在这里只剩「的冲销部分」
        assert "616,631.23" in text

    def test_empty_cell_is_explicit(self) -> None:
        """空单元格写「（空）」。

        实测：`本期金额` 本来就是空的，616,631.23 属于 `年初至报告期末金额`。
        不标出「本期为空」，模型会把数字算到别的列。
        """
        block = _table_block(
            [
                ["项目", "本期金额", "年初至报告期末金额"],
                ["营业收入", None, "113,977,728.63"],
            ]
        )
        text = chunk_document(_doc([block]))[0].text
        assert "营业收入 | （空） | 113,977,728.63" in text


class TestHeaderMerge:
    """多行表头逐列合并。"""

    def test_multi_row_header(self) -> None:
        block = _table_block(
            [
                ["项目", "本报告期", "本报告期比上年同期增减变动幅度"],
                [None, None, "(%)"],
                ["营业收入", "30,776,711.66", "17.75"],
            ],
            header_rows=2,
        )
        text = chunk_document(_doc([block]))[0].text
        assert "本报告期比上年同期增减变动幅度 (%)" in text
        assert "营业收入 | 30,776,711.66 | 17.75" in text

    def test_header_rows_respected(self) -> None:
        """header_rows=2 时，前两行都是表头，不能被当成数据。"""
        block = _table_block(
            [
                ["地区", "R&D 经费", "投入强度"],
                [None, "（亿元）", "（%）"],
                ["北京", "3696.1", "7.10"],
                ["上海", "2143.5", "4.55"],
            ],
            header_rows=2,
        )
        chunks = chunk_document(_doc([block]))
        assert len(chunks) == 1
        assert "北京 | 3696.1 | 7.10" in chunks[0].text
        assert "上海 | 2143.5 | 4.55" in chunks[0].text
        assert chunks[0].extra["row_count"] == 2


class TestNoFakeHeader:
    """无表头的表格不能编造假列名。"""

    def test_no_header_marker(self) -> None:
        block = _table_block(
            [
                ["王爱英", "境内自然人", "1,978,300"],
                ["李强", "境内自然人", "500,000"],
            ],
            header_rows=0,
        )
        text = chunk_document(_doc([block]))[0].text
        assert text.startswith("（本表无表头）")
        assert "王爱英" in text
        assert "列1" not in text, "不能出现「列1」这种假字段名"


class TestSubTableSplit:
    """一个 block 里装多张子表时，列名不能串（P8 实测）。

    股东表那张 block 实际装了两张表：

        r0: 股东名称 | 股东性质 | 持股数量 | 持股比例(%)
        r2-r11: 前10名股东数据
        r13: 股东名称 | 持有无限售条件流通股的数量 | 股份种类及数量
        r15+: 前10名无限售股东数据

    如果整块共用一个列名清单，第二张表会被贴上第一张表的列名。
    """

    def test_subtable_gets_own_chunk(self) -> None:
        block = Block(
            type=BlockType.TABLE,
            text="",
            page=1,
            table_index=5,
            header_rows=1,
            cells=[
                TableCell(text="股东名称", row=0, col=0, is_header=True),
                TableCell(text="持股数量", row=0, col=1, is_header=True),
                TableCell(text="西藏德锦", row=1, col=0),
                TableCell(text="55,307,275", row=1, col=1),
                # 第二张表的表头
                TableCell(text="股东名称", row=2, col=0),
                TableCell(text="股份种类", row=2, col=1),
                TableCell(text="西藏德锦", row=3, col=0),
                TableCell(text="人民币普通股", row=3, col=1),
            ],
        )
        chunks = chunk_document(_doc([block]))

        # 两张子表 → 两个块
        assert len(chunks) == 2

        assert "列：股东名称 / 持股数量" in chunks[0].text
        assert "西藏德锦 | 55,307,275" in chunks[0].text

        # 第二张表用自己的列名，不能串
        assert "列：股东名称 / 股份种类" in chunks[1].text
        assert "持股数量" not in chunks[1].text
        assert "西藏德锦 | 人民币普通股" in chunks[1].text

    def test_numeric_row_is_not_subheader(self) -> None:
        """带数字的数据行不能被误判成子表头。"""
        block = _table_block(
            [
                ["项目", "金额"],
                ["营业收入", "100"],
                ["利润总额", "50"],
            ]
        )
        chunks = chunk_document(_doc([block]))
        assert chunks[0].extra["row_count"] == 2, "数据行被当成子表头丢掉了"


class TestNoDuplication:
    """表格内容不能在正文块里重复出现。"""

    def test_pdf_table_not_in_paragraph(self) -> None:
        pytest.importorskip("pdfplumber")
        from pathlib import Path

        from app.core.paths import data_path
        from app.rag.parsers import parse_file

        pdf = data_path(
            "eval", "fixtures", "public",
            "上海汇通能源股份有限公司2026年第三季度报告.pdf",
        )
        if not Path(pdf).exists():
            pytest.skip("测试素材不存在")

        chunks = chunk_document(parse_file(pdf))
        hits = [c for c in chunks if "616,631.23" in c.text]

        # 必须恰好一块（旧实现有 2 块：paragraph + table_row）
        assert len(hits) == 1, (
            f"目标值出现在 {len(hits)} 个块里（应为 1）："
            f"{[(c.seq, c.chunk_type, len(c.text)) for c in hits]}"
        )
        assert "非流动性资产处置损益" in hits[0].text

    def test_pdf_body_not_lost(self) -> None:
        """排除表格区域后，正文不能丢。"""
        pytest.importorskip("pdfplumber")
        from pathlib import Path

        from app.core.paths import data_path
        from app.rag.parsers import parse_file

        pdf = data_path(
            "eval", "fixtures", "public",
            "上海汇通能源股份有限公司2026年第三季度报告.pdf",
        )
        if not Path(pdf).exists():
            pytest.skip("测试素材不存在")

        doc = parse_file(pdf)
        body = "".join(b.text for b in doc.blocks if b.type == "paragraph")
        for probe in ["是否经审计", "重要内容提示", "董事会", "编制单位"]:
            assert probe in body, f"正文丢失：{probe}"

    def test_shareholder_table_is_one_chunk(self) -> None:
        """股东表必须整表一块 —— 第一大股东与其余股东在同一块里。

        P8 第一版按行切，结果 n07（问第一大股东）的候选里
        **都没有西藏德锦 26.81%**，只有第 3 大和第 6 大股东。
        整表成块后，模型一次能看到全部 10 位股东。
        """
        pytest.importorskip("pdfplumber")
        from pathlib import Path

        from app.core.paths import data_path
        from app.rag.parsers import parse_file

        pdf = data_path(
            "eval", "fixtures", "public",
            "上海汇通能源股份有限公司2026年第三季度报告.pdf",
        )
        if not Path(pdf).exists():
            pytest.skip("测试素材不存在")

        chunks = chunk_document(parse_file(pdf))
        hits = [c for c in chunks if "西藏德锦" in c.text and "26.81" in c.text]
        assert hits, "找不到含第一大股东的块"

        text = hits[0].text
        # 同块内应当包含其他主要股东，这样才能判断「第一大」
        assert "厦门驱动力" in text
        assert "上海洪赢" in text
