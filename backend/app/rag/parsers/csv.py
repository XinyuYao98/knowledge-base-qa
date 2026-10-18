"""CSV 解析器（.csv / .tsv）。

测点：
- 编码识别（Excel 导出的 CSV 在中文 Windows 下常是 GBK）
- 分隔符自动判断（逗号 / 制表符 / 分号）
- 表头行识别
"""

from __future__ import annotations

import csv
import io
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

#: 候选分隔符，按常见程度排序
DELIMITERS = (",", "\t", ";", "|")


class CsvParser(BaseParser):
    extensions = ("csv", "tsv")
    format_name = "csv"
    mime = "text/csv"

    def _parse(self, path: Path, doc: ParsedDoc) -> None:
        raw = path.read_bytes()
        encoding, guessed = guess_encoding(raw, path.name)
        if guessed:
            doc.warn(WarnCode.ENCODING_GUESSED, f"CSV 编码非 UTF-8，按 {encoding} 解析")

        try:
            text = raw.decode(encoding, errors="replace")
        except Exception as e:
            raise ParseError(ParseErrorCode.CORRUPT_FILE, f"CSV 解码失败：{e}") from e

        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if not text.strip():
            raise ParseError(ParseErrorCode.EMPTY_FILE, "CSV 内容为空")

        dialect = self._sniff(text, path.suffix)
        doc.meta["delimiter"] = dialect
        doc.meta["encoding"] = encoding

        reader = csv.reader(io.StringIO(text), delimiter=dialect)
        rows: list[list[str]] = []
        for row in reader:
            cleaned = [c.strip() for c in row]
            if any(cleaned):
                rows.append(cleaned)

        if not rows:
            raise ParseError(ParseErrorCode.EMPTY_FILE, "CSV 中没有有效数据行")

        width = max(len(r) for r in rows)
        norm = [r + [""] * (width - len(r)) for r in rows]
        header = norm[0]
        flat = [h if h else f"列{i + 1}" for i, h in enumerate(header)]

        cells: list[TableCell] = []
        for c, name in enumerate(flat):
            cells.append(TableCell(text=name, row=0, col=c, is_header=True))
        for r, row in enumerate(norm[1:], start=1):
            for c, val in enumerate(row):
                if val:
                    cells.append(TableCell(text=val, row=r, col=c))

        lines = [" | ".join(flat)]
        for row in norm[1:]:
            lines.append(" | ".join(row))

        doc.blocks.append(
            Block(
                type=BlockType.TABLE,
                text="\n".join(lines),
                table_index=1,
                cells=cells,
                header_rows=1,
                col_span=f"A:{self._col_letter(width)}",
                extra={"row_count": len(norm) - 1, "col_count": width},
            )
        )

        doc.pages = 1
        doc.page_char_counts = [len(text)]
        doc.meta["table_count"] = 1

    # ------------------------------------------------------------------
    @staticmethod
    def _sniff(text: str, suffix: str) -> str:
        """判断分隔符。tsv 直接用制表符；csv 按首行出现次数投票。"""
        if suffix.lower() == ".tsv":
            return "\t"

        first_line = text.split("\n", 1)[0]
        best = ","
        best_count = 0
        for d in DELIMITERS:
            # 引号内的分隔符不该计入，但首行通常没有引号包裹的逗号，简化处理
            count = first_line.count(d)
            if count > best_count:
                best = d
                best_count = count
        return best

    @staticmethod
    def _col_letter(n: int) -> str:
        from openpyxl.utils import get_column_letter

        return get_column_letter(max(n, 1))
