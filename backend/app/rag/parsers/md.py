"""Markdown 解析器（.md / .markdown）。

测点：
- 标题层级（# / ## / ###）要转成 heading_path，供引用定位
- 代码块内容不能当正文
- 表格转成 Block(type=TABLE)
"""

from __future__ import annotations

import re
from pathlib import Path

from app.rag.parsers.base import (
    Block,
    BlockType,
    ParsedDoc,
    ParseError,
    ParseErrorCode,
    TableCell,
    WarnCode,
)
from app.rag.parsers.registry import BaseParser, guess_encoding

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.+)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")


def _split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


class MarkdownParser(BaseParser):
    extensions = ("md", "markdown", "mdx")
    format_name = "markdown"
    mime = "text/markdown"

    def _parse(self, path: Path, doc: ParsedDoc) -> None:
        raw = path.read_bytes()
        encoding, guessed = guess_encoding(raw, path.name)
        if guessed:
            doc.warn(
                WarnCode.ENCODING_GUESSED,
                f"文件编码非 UTF-8，已按 {encoding} 解析",
                encoding=encoding,
            )

        text = raw.decode(encoding, errors="replace")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        doc.meta["encoding"] = encoding

        if not text.strip():
            raise ParseError(ParseErrorCode.EMPTY_FILE, "文件内容为空")

        lines = text.split("\n")
        heading_stack: list[tuple[int, str]] = []
        table_index = 0
        i = 0
        para_buf: list[str] = []

        def flush_para() -> None:
            if para_buf:
                joined = "\n".join(para_buf).strip()
                if joined:
                    doc.blocks.append(
                        Block(
                            type=BlockType.PARAGRAPH,
                            text=joined,
                            heading_path=self._path_str(heading_stack),
                        )
                    )
                para_buf.clear()

        while i < len(lines):
            line = lines[i]

            # 代码块：整块收，标记 CODE，不当正文
            if _FENCE_RE.match(line):
                flush_para()
                fence = _FENCE_RE.match(line).group(1)  # type: ignore[union-attr]
                body: list[str] = []
                i += 1
                while i < len(lines) and not lines[i].strip().startswith(fence):
                    body.append(lines[i])
                    i += 1
                i += 1
                if body:
                    doc.blocks.append(
                        Block(
                            type=BlockType.CODE,
                            text="\n".join(body),
                            heading_path=self._path_str(heading_stack),
                        )
                    )
                continue

            # 标题
            m = _HEADING_RE.match(line)
            if m:
                flush_para()
                level = len(m.group(1))
                title = m.group(2).strip()
                while heading_stack and heading_stack[-1][0] >= level:
                    heading_stack.pop()
                heading_stack.append((level, title))
                doc.blocks.append(
                    Block(
                        type=BlockType.HEADING,
                        text=title,
                        heading_level=level,
                        heading_path=self._path_str(heading_stack),
                    )
                )
                i += 1
                continue

            # 表格：当前行含 | 且下一行是分隔行
            if "|" in line and i + 1 < len(lines) and _TABLE_SEP_RE.match(lines[i + 1]):
                flush_para()
                header = _split_row(line)
                i += 2
                rows: list[list[str]] = []
                while i < len(lines) and "|" in lines[i] and lines[i].strip():
                    rows.append(_split_row(lines[i]))
                    i += 1
                table_index += 1
                cells: list[TableCell] = []
                for c, txt in enumerate(header):
                    cells.append(TableCell(text=txt, row=0, col=c, is_header=True))
                for r, row in enumerate(rows, start=1):
                    for c, txt in enumerate(row):
                        cells.append(TableCell(text=txt, row=r, col=c))
                flat = " | ".join(header) + "\n" + "\n".join(" | ".join(r) for r in rows)
                doc.blocks.append(
                    Block(
                        type=BlockType.TABLE,
                        text=flat,
                        heading_path=self._path_str(heading_stack),
                        table_index=table_index,
                        cells=cells,
                        header_rows=1,
                        extra={"row_count": len(rows), "col_count": len(header)},
                    )
                )
                continue

            # 列表项
            lm = _LIST_RE.match(line)
            if lm:
                flush_para()
                doc.blocks.append(
                    Block(
                        type=BlockType.LIST_ITEM,
                        text=lm.group(1).strip(),
                        heading_path=self._path_str(heading_stack),
                    )
                )
                i += 1
                continue

            # 空行 = 段落边界
            if not line.strip():
                flush_para()
                i += 1
                continue

            para_buf.append(line)
            i += 1

        flush_para()

        doc.pages = None
        doc.page_char_counts = []
        doc.meta["table_count"] = table_index

    @staticmethod
    def _path_str(stack: list[tuple[int, str]]) -> str:
        return " > ".join(t for _, t in stack)
