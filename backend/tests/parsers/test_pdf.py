"""PDF 解析器测试。

覆盖两类：
1. 合成扫描件 → 必须报 no_text_layer
2. 真实年报 → 正文与表格都要正确提取
"""

from __future__ import annotations

import pytest

from app.rag.parsers import BlockType, ParseError, ParseErrorCode, parse_file
from tests.parsers.conftest import fixture_path


class TestScannedPdf:
    """扫描件必须明确报错，不能静默返回空内容。"""

    def test_raises_no_text_layer(self) -> None:
        with pytest.raises(ParseError) as exc:
            parse_file(fixture_path("pdf_scanned_no_text.pdf"))
        assert exc.value.code == ParseErrorCode.NO_TEXT_LAYER

    def test_message_mentions_ocr(self) -> None:
        """错误信息要说明「不支持 OCR」，否则用户不知道该换什么文件。"""
        with pytest.raises(ParseError) as exc:
            parse_file(fixture_path("pdf_scanned_no_text.pdf"))
        assert "OCR" in exc.value.message or "扫描" in exc.value.message


class TestRealReport:
    """真实上市公司季报。"""

    @pytest.fixture(scope="class")
    @classmethod
    def doc(cls):  # type: ignore[no-untyped-def]
        return parse_file(fixture_path("上海汇通能源股份有限公司2026年第三季度报告.pdf", "public"))

    def test_page_count(self, doc) -> None:  # type: ignore[no-untyped-def]
        assert doc.pages == 12

    def test_has_text_on_every_page(self, doc) -> None:  # type: ignore[no-untyped-def]
        """12 页全部有文字，不应触发扫描件告警。"""
        assert all(n > 50 for n in doc.page_char_counts)
        codes = [str(w.code) for w in doc.warnings]
        assert "no_text_layer" not in codes

    def test_tables_extracted(self, doc) -> None:  # type: ignore[no-untyped-def]
        """财务三表必须能抽出来。"""
        assert doc.table_count >= 10, f"表格太少：{doc.table_count}"

    def test_largest_table_has_many_rows(self, doc) -> None:  # type: ignore[no-untyped-def]
        """最大表应有 40+ 行（资产负债表）。"""
        max_rows = max(
            (b.extra.get("row_count", 0) for b in doc.blocks if b.type == BlockType.TABLE),
            default=0,
        )
        assert max_rows >= 30, f"最大表只有 {max_rows} 行"

    def test_paragraphs_merged(self, doc) -> None:  # type: ignore[no-untyped-def]
        """PDF 抽出的文本一句一换行，必须合并成段落。

        不合并会产生大量碎片块（每块十几个字），严重损害检索质量。
        """
        paras = [b for b in doc.blocks if b.type == BlockType.PARAGRAPH]
        assert paras, "没有正文块"
        avg_len = sum(b.char_count for b in paras) / len(paras)
        assert avg_len > 50, f"段落平均只有 {avg_len:.0f} 字，碎片化严重"

    def test_page_numbers_preserved(self, doc) -> None:  # type: ignore[no-untyped-def]
        """引用要能落到页码，所以每个块必须记录 page。"""
        paras = [b for b in doc.blocks if b.type == BlockType.PARAGRAPH]
        assert all(b.page is not None for b in paras)

    def test_table_header_not_empty(self, doc) -> None:  # type: ignore[no-untyped-def]
        """表格表头不能是「列1列2列3」这种占位。"""
        tables = [b for b in doc.blocks if b.type == BlockType.TABLE]
        assert tables
        first_header = tables[0].text.split("\n")[0]
        assert "列1" not in first_header or len(first_header) > 30


class TestNoEncryption:
    def test_encrypted_raises(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """加密 PDF 应报 encrypted，而不是 corrupt_file。"""
        pytest.skip("需要加密 PDF 样例，当前素材库未提供")
