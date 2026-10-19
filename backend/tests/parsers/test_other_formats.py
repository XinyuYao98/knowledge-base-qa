"""PPT / DOCX / Markdown / CSV / TXT / HTML 解析器测试。"""

from __future__ import annotations

import pytest

from app.rag.parsers import BlockType, ParseError, ParseErrorCode, parse_file
from tests.parsers.conftest import fixture_path


class TestPptxNotes:
    """PPT 备注提取 —— 正文极简、真信息在备注里。"""

    @pytest.fixture(scope="class")
    @classmethod
    def doc(cls):  # type: ignore[no-untyped-def]
        return parse_file(fixture_path("pptx_notes_heavy.pptx"))

    def test_slide_count(self, doc) -> None:  # type: ignore[no-untyped-def]
        assert doc.pages == 3

    def test_notes_extracted(self, doc) -> None:  # type: ignore[no-untyped-def]
        """备注内容必须进块，否则 80% 信息丢失。"""
        full = doc.text_only()
        assert "8600 万元" in full, "第 1 页备注的关键数字丢了"
        assert "12 个地级市" in full, "第 2 页备注内容丢了"
        assert "14 周" in full, "第 3 页备注内容丢了"

    def test_notes_marked(self, doc) -> None:  # type: ignore[no-untyped-def]
        """备注要有标记，让读者知道这段来自备注而非页面。"""
        full = doc.text_only()
        assert "【备注】" in full

    def test_notes_longer_than_body(self, doc) -> None:  # type: ignore[no-untyped-def]
        assert doc.meta["notes_chars"] > doc.meta["body_chars"]

    def test_slide_number_preserved(self, doc) -> None:  # type: ignore[no-untyped-def]
        pages = {b.page for b in doc.blocks if b.page is not None}
        assert pages == {1, 2, 3}


class TestDocx:
    @pytest.fixture(scope="class")
    @classmethod
    def doc(cls):  # type: ignore[no-untyped-def]
        return parse_file(fixture_path("docx_heading_table.docx"))

    def test_heading_hierarchy(self, doc) -> None:  # type: ignore[no-untyped-def]
        headings = [b for b in doc.blocks if b.type == BlockType.HEADING]
        assert len(headings) >= 4
        levels = {h.heading_level for h in headings}
        assert levels <= {1, 2, 3}

    def test_heading_path(self, doc) -> None:  # type: ignore[no-untyped-def]
        """正文段落必须带 heading_path，引用时才能说清「第几章第几节」。"""
        para = next(
            b for b in doc.blocks if b.type == BlockType.PARAGRAPH and "480" in b.text
        )
        assert "第一章 差旅管理" in para.heading_path
        assert "1.1 住宿标准" in para.heading_path

    def test_table_extracted(self, doc) -> None:  # type: ignore[no-untyped-def]
        assert doc.table_count == 1
        table = next(b for b in doc.blocks if b.type == BlockType.TABLE)
        assert "住宿上限" in table.text
        assert "480" in table.text

    def test_list_items(self, doc) -> None:  # type: ignore[no-untyped-def]
        items = [b for b in doc.blocks if b.type == BlockType.LIST_ITEM]
        assert len(items) >= 3


class TestMarkdown:
    @pytest.fixture(scope="class")
    @classmethod
    def doc(cls):  # type: ignore[no-untyped-def]
        return parse_file(fixture_path("md_with_table.md"))

    def test_headings(self, doc) -> None:  # type: ignore[no-untyped-def]
        headings = [b for b in doc.blocks if b.type == BlockType.HEADING]
        titles = [h.text for h in headings]
        assert "青岚智造 API 使用说明" in titles

    def test_code_block_separated(self, doc) -> None:  # type: ignore[no-untyped-def]
        """代码块单独成块，不能和正文混在一起。"""
        codes = [b for b in doc.blocks if b.type == BlockType.CODE]
        assert codes
        assert "POST /api/v1/telemetry" in codes[0].text

    def test_table(self, doc) -> None:  # type: ignore[no-untyped-def]
        assert doc.table_count == 1
        table = next(b for b in doc.blocks if b.type == BlockType.TABLE)
        assert "device_id" in table.text
        assert "字段" in table.text

    def test_list_items(self, doc) -> None:  # type: ignore[no-untyped-def]
        items = [b for b in doc.blocks if b.type == BlockType.LIST_ITEM]
        assert any("401" in b.text for b in items)


class TestCsv:
    @pytest.fixture(scope="class")
    @classmethod
    def doc(cls):  # type: ignore[no-untyped-def]
        return parse_file(fixture_path("csv_gbk_export.csv"))

    def test_gbk_decoded(self, doc) -> None:  # type: ignore[no-untyped-def]
        """Excel 导出的 CSV 在中文 Windows 下常是 GBK。"""
        full = doc.text_only()
        assert "设备编号" in full, "中文乱码 —— 编码识别失败"
        assert "\ufffd" not in full, "出现替换字符，说明解码错误"

    def test_delimiter_detected(self, doc) -> None:  # type: ignore[no-untyped-def]
        assert doc.meta["delimiter"] == ","

    def test_rows(self, doc) -> None:  # type: ignore[no-untyped-def]
        table = next(b for b in doc.blocks if b.type == BlockType.TABLE)
        assert table.extra["row_count"] == 4
        assert "GW-001" in table.text


class TestTxt:
    @pytest.fixture(scope="class")
    @classmethod
    def doc(cls):  # type: ignore[no-untyped-def]
        return parse_file(fixture_path("txt_gbk_chinese.txt"))

    def test_gbk_detected(self, doc) -> None:  # type: ignore[no-untyped-def]
        assert doc.meta["encoding"] in ("gb18030", "gbk", "gb2312")

    def test_chinese_intact(self, doc) -> None:  # type: ignore[no-untyped-def]
        full = doc.text_only()
        assert full.startswith("青岚智造")
        assert "480 元" in full
        assert "20 万元" in full

    def test_paragraphs(self, doc) -> None:  # type: ignore[no-untyped-def]
        assert doc.block_count >= 3

    def test_encoding_warning(self, doc) -> None:  # type: ignore[no-untyped-def]
        codes = [str(w.code) for w in doc.warnings]
        assert "encoding_guessed" in codes


class TestEmpty:
    def test_empty_file_rejected(self) -> None:
        with pytest.raises(ParseError) as exc:
            parse_file(fixture_path("empty.txt"))
        assert exc.value.code == ParseErrorCode.EMPTY_FILE


class TestLegacyFormats:
    @pytest.mark.parametrize("ext,hint", [("doc", "另存为"), ("xls", "另存为"), ("ppt", "另存为")])
    def test_legacy_rejected_with_hint(self, tmp_path, ext: str, hint: str) -> None:  # type: ignore[no-untyped-def]
        """老格式必须拒绝，且提示要可操作（告诉用户怎么转）。"""
        f = tmp_path / f"legacy.{ext}"
        f.write_bytes(b"fake content " * 10)
        with pytest.raises(ParseError) as exc:
            parse_file(f)
        assert exc.value.code == ParseErrorCode.LEGACY_FORMAT
        assert hint in exc.value.message


class TestUnsupported:
    def test_unknown_ext(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        f = tmp_path / "file.xyz"
        f.write_bytes(b"data")
        with pytest.raises(ParseError) as exc:
            parse_file(f)
        assert exc.value.code == ParseErrorCode.UNSUPPORTED_FORMAT


class TestHtml:
    """真实国家统计局页面（浏览器另存，带噪声）。"""

    @pytest.fixture(scope="class")
    @classmethod
    def doc(cls):  # type: ignore[no-untyped-def]
        return parse_file(
            fixture_path("2025年全国科技经费投入统计公报 - 国家统计局.html", "public")
        )

    def test_tables_found(self, doc) -> None:  # type: ignore[no-untyped-def]
        assert doc.table_count == 6, f"应有 6 张表，实际 {doc.table_count}"

    def test_large_tables(self, doc) -> None:  # type: ignore[no-untyped-def]
        rows = sorted(
            (b.extra.get("row_count", 0) for b in doc.blocks if b.type == BlockType.TABLE),
            reverse=True,
        )
        assert rows[0] >= 40, f"最大的表只有 {rows[0]} 行"

    def test_noise_removed(self, doc) -> None:  # type: ignore[no-untyped-def]
        """script/style 内容不能进正文。"""
        full = doc.text_only()
        assert "function(" not in full
        assert "var " not in full

    def test_title_captured(self, doc) -> None:  # type: ignore[no-untyped-def]
        assert "科技经费投入统计公报" in doc.meta.get("title", "")

    def test_headings(self, doc) -> None:  # type: ignore[no-untyped-def]
        headings = [b for b in doc.blocks if b.type == BlockType.HEADING]
        assert len(headings) >= 1
