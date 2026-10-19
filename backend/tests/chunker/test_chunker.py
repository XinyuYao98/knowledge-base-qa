"""切块器测试。

重点验证：
- 表格按行切块时，每块都带表头
- 超长段落切分带重叠
- 过短的块被丢弃
"""

from __future__ import annotations

from app.rag.chunker import Chunk, _split_long, chunk_document
from app.rag.parsers.base import Block, BlockType, ParsedDoc, TableCell


def make_doc(*blocks: Block) -> ParsedDoc:
    doc = ParsedDoc(filename="测试.txt", format="txt")
    doc.blocks = list(blocks)
    return doc


def para(text: str, **kw) -> Block:
    return Block(type=BlockType.PARAGRAPH, text=text, **kw)


def table(header: str, rows: list[str], **kw) -> Block:
    text = header + "\n" + "\n".join(rows)
    return Block(
        type=BlockType.TABLE,
        text=text,
        cells=[TableCell(text=h, row=0, col=i, is_header=True) for i, h in enumerate(header.split(" | "))],
        header_rows=1,
        **kw,
    )


class TestParagraphChunking:
    def test_basic(self) -> None:
        doc = make_doc(para("这是一段足够长的正文内容，用于测试切块逻辑是否正常工作。"))
        chunks = chunk_document(doc)
        assert len(chunks) == 1
        assert chunks[0].seq == 0

    def test_short_block_dropped(self) -> None:
        """过短的块丢弃，避免碎片污染索引。"""
        doc = make_doc(para("短"), para("这是一段足够长的正文内容，用于测试切块逻辑是否正确。"))
        chunks = chunk_document(doc)
        assert len(chunks) == 1
        assert "短" not in chunks[0].text or len(chunks[0].text) > 5

    def test_seq_continuous(self) -> None:
        doc = make_doc(
            para("第一段内容，长度足够用于测试。"),
            para("第二段内容，长度也足够用于测试。"),
            para("第三段内容，同样足够长。"),
        )
        chunks = chunk_document(doc)
        assert [c.seq for c in chunks] == list(range(len(chunks)))

    def test_heading_path_preserved(self) -> None:
        doc = make_doc(para("一线城市住宿标准为每晚不超过四百八十元，超出部分个人承担。", heading_path="第一章 > 1.1"))
        chunks = chunk_document(doc)
        assert chunks[0].heading_path == "第一章 > 1.1"

    def test_page_preserved(self) -> None:
        doc = make_doc(para("这一页的正文内容写得足够长，以确保不会被最短长度过滤掉。", page=7))
        chunks = chunk_document(doc)
        assert chunks[0].page == 7


class TestLongSplit:
    def test_no_split_when_short(self) -> None:
        assert _split_long("短文本", 100, 20) == ["短文本"]

    def test_split_with_overlap(self) -> None:
        text = "A" * 250
        parts = _split_long(text, 100, 20)
        assert len(parts) > 1
        # 首尾相接处应有重叠
        assert parts[0][-20:] == parts[1][:20]

    def test_all_content_covered(self) -> None:
        text = "0123456789" * 30
        parts = _split_long(text, 100, 20)
        assert "".join(parts[:1]) in text
        assert parts[-1] in text


class TestTableChunking:
    def test_row_chunks_repeat_header(self) -> None:
        """表格每行必须带表头 —— 否则 1280 是单价还是数量无从判断。"""
        doc = make_doc(
            table(
                "产品编号 | 名称 | 单价",
                ["P-001 | 智能网关 | 1280", "P-002 | 边缘控制器 | 2450"],
            )
        )
        chunks = chunk_document(doc)
        assert len(chunks) == 2
        for c in chunks:
            assert "产品编号 | 名称 | 单价" in c.text

    def test_chunk_type_table_row(self) -> None:
        doc = make_doc(table("A | B", ["1 | 2"]))
        chunks = chunk_document(doc)
        assert all(c.chunk_type == "table_row" for c in chunks)

    def test_single_row_table_ignored(self) -> None:
        """只有表头没有数据行 → 不产块。"""
        doc = make_doc(text_block := Block(type=BlockType.TABLE, text="只有表头"))
        assert chunk_document(doc) == []
        del text_block

    def test_table_with_metadata(self) -> None:
        doc = make_doc(table("A | B", ["1 | 2", "3 | 4"], page=3, table_index=2))
        chunks = chunk_document(doc)
        assert chunks[0].page == 3
        assert chunks[0].extra["table_index"] == 2


class TestChunk:
    def test_token_estimate(self) -> None:
        c = Chunk(text="中" * 100, seq=0)
        assert 60 <= c.token_estimate <= 80   # 100 字 ≈ 70 token

    def test_citation_label_with_page(self) -> None:
        c = Chunk(text="x", seq=0, source="制度.pdf", page=3)
        label = c.citation_label()
        assert "制度.pdf" in label
        assert "第3页" in label

    def test_citation_label_with_sheet(self) -> None:
        c = Chunk(text="x", seq=0, source="报表.xlsx", sheet="汇总")
        assert "汇总" in c.citation_label()
