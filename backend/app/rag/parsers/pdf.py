"""PDF 解析器（.pdf）。

## 这是本项目技术风险最高的一块

pypdf 与 pdfplumber 各有强弱：

| 能力 | pypdf | pdfplumber |
|---|---|---|
| 纯文本抽取 | 快，版面顺序基本可用 | 慢，但按坐标排列更准 |
| 表格检测 | 不支持 | 支持（基于线条/文字对齐） |
| 记忆占用 | 低 | 高（保留每个字符坐标） |

策略：**pypdf 抽正文，pdfplumber 抽表格**。
两者都跑，正文与表格分别产出块。这样可以避免 pdfplumber 拖慢大文件。

## 已知限制（必须在 README 写明）

1. **双栏排版可能读错顺序**：pypdf 按流的物理顺序输出，不按视觉分栏。
2. **扫描件无文字层**：直接抛 no_text_layer，不做 OCR。
3. **复杂表格（合并单元格 / 无线条）**：pdfplumber 可能识别失败或错位。
"""

from __future__ import annotations

from pathlib import Path

import pdfplumber
from pypdf import PdfReader
from pypdf.errors import PdfReadError

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


class PdfParser(BaseParser):
    extensions = ("pdf",)
    format_name = "pdf"
    mime = "application/pdf"

    #: 每页字符少于此值视为"这页没有文字层"
    LOW_TEXT_PER_PAGE = 50

    def _parse(self, path: Path, doc: ParsedDoc) -> None:
        # ---- 1. pypdf 读正文 ----
        try:
            reader = PdfReader(str(path))
        except PdfReadError as e:
            raise ParseError(
                ParseErrorCode.CORRUPT_FILE,
                f"PDF 文件损坏或格式不合法：{e}",
            ) from e
        except Exception as e:
            raise ParseError(
                ParseErrorCode.CORRUPT_FILE,
                f"无法打开 PDF：{type(e).__name__}",
                detail={"error": str(e)[:200]},
            ) from e

        if getattr(reader, "is_encrypted", False):
            raise ParseError(
                ParseErrorCode.ENCRYPTED,
                "PDF 已加密，请先解除密码保护再上传",
            )

        page_count = len(reader.pages)
        doc.pages = page_count
        doc.meta["page_count"] = page_count

        if page_count == 0:
            raise ParseError(ParseErrorCode.EMPTY_FILE, "PDF 没有页面")

        page_texts: list[str] = []
        for page in reader.pages:
            try:
                txt = page.extract_text() or ""
            except Exception:
                txt = ""
            page_texts.append(txt)
            doc.page_char_counts.append(len(txt.strip()))

        # 扫描件判定交给 BaseParser._finalize，这里只填 page_char_counts

        # ---- 2. pdfplumber 抽表格 ----
        tables_by_page: dict[int, list[list[list[str | None]]]] = {}
        try:
            with pdfplumber.open(str(path)) as plumber:
                for i, page in enumerate(plumber.pages, start=1):
                    try:
                        found = page.extract_tables()
                    except Exception as e:
                        doc.warn(
                            WarnCode.TABLE_DETECT_FAILED,
                            f"第 {i} 页表格识别失败：{type(e).__name__}",
                            page=i,
                        )
                        continue
                    good = [t for t in found if t and len(t) > 1]
                    if good:
                        tables_by_page[i] = good
        except Exception as e:
            doc.warn(
                WarnCode.TABLE_DETECT_FAILED,
                f"表格抽取整体失败，仅保留正文：{type(e).__name__}",
            )

        # ---- 3. 逐页产出块：正文段落 + 表格 ----
        table_counter = 0
        for page_no, text in enumerate(page_texts, start=1):
            # 正文：按空行分段
            for para in self._split_paragraphs(text):
                doc.blocks.append(
                    Block(type=BlockType.PARAGRAPH, text=para, page=page_no)
                )

            # 该页的表格
            for tbl in tables_by_page.get(page_no, []):
                table_counter += 1
                header_rows, flat_header, data_rows = self._normalize_table(tbl)

                cells: list[TableCell] = []
                for c, name in enumerate(flat_header):
                    cells.append(TableCell(text=name, row=0, col=c, is_header=True))
                for r, row in enumerate(data_rows, start=1):
                    for c, val in enumerate(row):
                        if val is None or str(val).strip() == "":
                            continue
                        cells.append(TableCell(text=str(val).strip(), row=r, col=c))

                lines = [" | ".join(flat_header)]
                for row in data_rows:
                    lines.append(" | ".join((v or "").strip() for v in row))

                doc.blocks.append(
                    Block(
                        type=BlockType.TABLE,
                        text="\n".join(lines),
                        page=page_no,
                        table_index=table_counter,
                        cells=cells,
                        header_rows=header_rows,
                        extra={
                            "page": page_no,
                            "row_count": len(data_rows),
                            "col_count": len(flat_header),
                        },
                    )
                )

        doc.meta["table_count"] = table_counter

    # ------------------------------------------------------------------
    @staticmethod
    def _split_paragraphs(text: str) -> list[str]:
        """按空行分段，并合并被硬换行打断的句子。

        PDF 抽出的文本经常一句一换行，如果直接按行切，
        会产生大量碎片块（每块只有十几个字），严重影响检索质量。
        """
        if not text.strip():
            return []

        lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").split("\n")]
        paras: list[str] = []
        buf: list[str] = []

        for line in lines:
            stripped = line.strip()
            if not stripped:
                if buf:
                    paras.append(" ".join(buf))
                    buf = []
                continue
            # 短行（可能是标题）且前一段以句末标点结尾 → 新段
            if (
                len(stripped) < 20
                and buf
                and buf[-1].endswith(("。", "！", "？", ".", "!", "?", "；", ";"))
            ):
                paras.append(" ".join(buf))
                buf = []
            buf.append(stripped)

        if buf:
            paras.append(" ".join(buf))

        # 合并过短的段落到前一段
        merged: list[str] = []
        for p in paras:
            if merged and len(p) < 15:
                merged[-1] = merged[-1] + " " + p
            else:
                merged.append(p)

        return [p.strip() for p in merged if p.strip()]

    @staticmethod
    def _normalize_table(
        raw: list[list[str | None]],
    ) -> tuple[int, list[str], list[list[str | None]]]:
        """规范表格：判断表头行数，压平字段名，去全空行。"""
        # 去掉整行为空的行
        rows = [r for r in raw if any((c or "").strip() for c in r)]
        if not rows:
            return 0, [], []

        width = max(len(r) for r in rows)
        norm: list[list[str | None]] = [
            list(r) + [None] * (width - len(r)) for r in rows
        ]

        # 表头行数：第一行必是表头；若第二行与第一行"错位互补"则为双行
        header_n = 1
        if len(norm) >= 2:
            first, second = norm[0], norm[1]
            empty_in_first = sum(1 for c in first if not (c or "").strip())
            filled_in_second = sum(
                1 for i, c in enumerate(second)
                if (c or "").strip() and not (first[i] or "").strip()
            )
            if empty_in_first > 0 and filled_in_second >= max(1, empty_in_first // 2):
                header_n = 2

        flat: list[str] = []
        for c in range(width):
            parts: list[str] = []
            for r in range(header_n):
                v = norm[r][c]
                s = (v or "").strip()
                if s and (not parts or parts[-1] != s):
                    parts.append(s)
            flat.append("-".join(parts) if parts else f"列{c + 1}")

        return header_n, flat, norm[header_n:]
