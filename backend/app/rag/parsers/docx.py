"""Word 解析器（.docx）。

测点：
- 标题样式（Heading 1-6）→ heading_path
- 表格转 Block(type=TABLE)
- 列表项
- 老格式 .doc 明确拒绝（不装 LibreOffice）

## 已知限制

- **不提取批注**（python-docx 对 comments 支持有限）
- **不提取图片内文字**（无 OCR）
- 页眉页脚默认跳过（重复内容进索引会污染检索）
"""

from __future__ import annotations

from pathlib import Path

from docx import Document as DocxDocument
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph as DocxParagraph

from app.rag.parsers.base import (
    Block,
    BlockType,
    ParsedDoc,
    ParseError,
    ParseErrorCode,
    TableCell,
    WarnCode,
)
from app.rag.parsers.registry import BaseParser


class DocxParser(BaseParser):
    extensions = ("docx",)
    format_name = "docx"
    mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    def _parse(self, path: Path, doc: ParsedDoc) -> None:
        try:
            document = DocxDocument(str(path))
        except Exception as e:
            raise ParseError(
                ParseErrorCode.CORRUPT_FILE,
                f"无法打开 Word 文件：{type(e).__name__}",
                detail={"error": str(e)[:200]},
            ) from e

        heading_stack: list[tuple[int, str]] = []
        table_counter = 0

        # 按文档顺序遍历段落与表格（python-docx 需要手动合并两个迭代器）
        for item in self._iter_block_items(document):
            if isinstance(item, DocxParagraph):
                text = item.text.strip()
                if not text:
                    continue

                style = (item.style.name or "") if item.style is not None else ""
                level = self._heading_level(style)

                if level:
                    while heading_stack and heading_stack[-1][0] >= level:
                        heading_stack.pop()
                    heading_stack.append((level, text))
                    doc.blocks.append(
                        Block(
                            type=BlockType.HEADING,
                            text=text,
                            heading_level=level,
                            heading_path=self._path(heading_stack),
                        )
                    )
                elif style.lower().startswith("list"):
                    doc.blocks.append(
                        Block(
                            type=BlockType.LIST_ITEM,
                            text=text,
                            heading_path=self._path(heading_stack),
                        )
                    )
                else:
                    doc.blocks.append(
                        Block(
                            type=BlockType.PARAGRAPH,
                            text=text,
                            heading_path=self._path(heading_stack),
                        )
                    )

            elif isinstance(item, DocxTable):
                table_counter += 1
                block = self._table_to_block(item, table_counter, heading_stack)
                if block is not None:
                    doc.blocks.append(block)

        # 检查是否有图片（不提取内容，但要告警）
        image_count = self._count_images(document)
        if image_count:
            doc.warn(
                WarnCode.IMAGE_SKIPPED,
                f"文档含 {image_count} 张图片，图片内文字未提取（不支持 OCR）",
                count=image_count,
            )

        if not doc.blocks:
            raise ParseError(ParseErrorCode.EMPTY_FILE, "文档中没有可提取的文字")

        doc.pages = None
        doc.page_char_counts = []
        doc.meta["table_count"] = table_counter
        doc.meta["image_count"] = image_count

    # ------------------------------------------------------------------
    @staticmethod
    def _iter_block_items(parent):  # type: ignore[no-untyped-def]
        """按文档顺序产出段落与表格。"""
        from docx.oxml.ns import qn

        body = parent.element.body
        for child in body.iterchildren():
            if child.tag == qn("w:p"):
                yield DocxParagraph(child, parent)
            elif child.tag == qn("w:tbl"):
                yield DocxTable(child, parent)

    @staticmethod
    def _heading_level(style_name: str) -> int | None:
        """从样式名推断标题级别。支持中英文样式名。"""
        name = style_name.strip()
        if not name:
            return None
        # Heading 1 / 标题 1
        for prefix in ("Heading ", "标题 "):
            if name.startswith(prefix):
                try:
                    lvl = int(name[len(prefix):].strip())
                    return min(max(lvl, 1), 6)
                except ValueError:
                    return None
        if name in ("Title", "标题"):
            return 1
        return None

    def _table_to_block(
        self,
        table: DocxTable,
        index: int,
        heading_stack: list[tuple[int, str]],
    ) -> Block | None:
        rows: list[list[str]] = []
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                rows.append(cells)

        if not rows:
            return None

        width = max(len(r) for r in rows)
        norm = [r + [""] * (width - len(r)) for r in rows]

        header_n = 1
        if len(norm) >= 2:
            first, second = norm[0], norm[1]
            empty_first = sum(1 for c in first if not c)
            filled_second = sum(1 for i, c in enumerate(second) if c and not first[i])
            if empty_first and filled_second >= max(1, empty_first // 2):
                header_n = 2

        flat: list[str] = []
        for c in range(width):
            parts = [norm[r][c] for r in range(header_n) if norm[r][c]]
            flat.append("-".join(dict.fromkeys(parts)) if parts else f"列{c + 1}")

        cells: list[TableCell] = []
        for c, name in enumerate(flat):
            cells.append(TableCell(text=name, row=0, col=c, is_header=True))
        for r, row in enumerate(norm[header_n:], start=1):
            for c, val in enumerate(row):
                if val:
                    cells.append(TableCell(text=val, row=r, col=c))

        lines = [" | ".join(flat)]
        for row in norm[header_n:]:
            lines.append(" | ".join(row))

        return Block(
            type=BlockType.TABLE,
            text="\n".join(lines),
            heading_path=self._path(heading_stack),
            table_index=index,
            cells=cells,
            header_rows=header_n,
            extra={"row_count": len(norm) - header_n, "col_count": width},
        )

    @staticmethod
    def _count_images(document) -> int:  # type: ignore[no-untyped-def]
        try:
            return len(document.inline_shapes)
        except Exception:
            return 0

    @staticmethod
    def _path(stack: list[tuple[int, str]]) -> str:
        return " > ".join(t for _, t in stack)


class LegacyDocParser(BaseParser):
    """老格式 .doc：明确拒绝，给出可操作的提示。"""

    extensions = ("doc",)
    format_name = "doc"
    mime = "application/msword"

    def _parse(self, path: Path, doc: ParsedDoc) -> None:
        raise ParseError(
            ParseErrorCode.LEGACY_FORMAT,
            "不支持 .doc 老格式。请用 Word 或 WPS 另存为 .docx 后重新上传。",
        )
