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
from typing import Any

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

#: 纯数字单元格：可带千分位、小数点、负号、百分号、括号负数
_NUM_RE = __import__("re").compile(r"^[-(（]?[-\d,]+(?:\.\d+)?[)）]?%?$")


def _looks_like_data_row(row: list[str | None]) -> bool:
    """判断一行是否「像数据行」（用于表头识别）。

    ## 判据：数字单元格占比

    表头几乎不含纯数字：「项目 / 本报告期 / 金额」。
    数据行往往一半以上是数字：「营业收入 / 30,776,711.66 / 17.75」。

    只要**有任意一个纯数字单元格**就认为不是表头 ——
    实测利润表续页的第一行是
    `（一）按经营持续性分类 | 39,998,764.22 | 26,289,237.30`，
    只有两个数字格子，但已经足够判定它是数据。

    宁可少判表头，也不能把数据当列名丢掉。
    """
    filled = [str(c).strip() for c in row if c and str(c).strip()]
    if not filled:
        return False
    return any(_NUM_RE.match(v) for v in filled)


#: 表头单元格的典型最大长度。超过这个长度的多半是数据（项目名、股东名）
_HEADER_CELL_MAX = 12


def _looks_like_header_row(row: list[str | None], width: int) -> bool:
    """判断一行是否「像列名行」。

    ## 判据（三条同时满足）

    1. **不含纯数字**：「股东名称 / 持股数量 / 持股比例」没有数字，
       而「13,587」「30,776,711.66」有。
    2. **填充率够高**：至少一半的列有值。列名行各列都有名字；
       数据行经常因为合并单元格只填首列。
    3. **单元格够短**：列名一般 ≤ 12 字。像
       「非流动性资产处置损益，包括已计提资产减值准备的冲销部分」
       这种长句是数据。

    三条都满足才认为可能是列名 —— 宁可判不出表头（走无表头兜底），
    也不要把一段数据当成列名贴到每个数字前面。
    """
    filled = [str(c).strip() for c in row if c and str(c).strip()]
    if not filled:
        return False
    if any(_NUM_RE.match(v) for v in filled):
        return False
    if len(filled) * 2 < max(1, width):
        return False
    avg_len = sum(len(v) for v in filled) / len(filled)
    return avg_len <= _HEADER_CELL_MAX


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
        #: 每页的表格 bbox，用于从正文里排除表格区域
        table_bboxes: dict[int, list[tuple[float, float, float, float]]] = {}
        #: 每页「排除表格后」的正文文本
        body_by_page: dict[int, str] = {}
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
                    if not good:
                        continue
                    tables_by_page[i] = good

                    # 取 bbox 并反过来排除表格区域，抽剩余正文。
                    #
                    # ## 为什么必须排除（P7 实测）
                    #
                    # `pypdf.extract_text()` 会把表格内容一起抽成文字。
                    # 如果只靠 pypdf 分段，一张表会出现两次：
                    #
                    #   blocks[5] paragraph  ← 整张表被压成一段，列边界全丢
                    #                         「...冲销部分 616,631.23 计入...」
                    #   blocks[6] table      ← 结构化版本（cells 完整）
                    #
                    # 那个 paragraph 是**最坏的一种块**：文本全在、但没有列对齐，
                    # 模型看到一串数字不知道哪个对应哪个字段。
                    # 而且它和 table_row 块信息重复，会挤占 top-5 候选名额。
                    #
                    # 按 bbox 排除后，正文块和表格行块各自干净，不再重叠。
                    try:
                        bboxes = [tuple(t.bbox) for t in page.find_tables()]
                        table_bboxes[i] = bboxes  # type: ignore[assignment]
                        body_by_page[i] = _text_outside_boxes(page, bboxes)
                    except Exception:
                        # 排除失败就保留原文 —— 宁可重复，不能丢内容
                        body_by_page[i] = page_texts[i - 1]
        except Exception as e:
            doc.warn(
                WarnCode.TABLE_DETECT_FAILED,
                f"表格抽取整体失败，仅保留正文：{type(e).__name__}",
            )

        # ---- 3. 逐页产出块：正文段落 + 表格 ----
        table_counter = 0
        for page_no, text in enumerate(page_texts, start=1):
            # 正文用「排除表格后」的版本；没有表格的页原样使用
            body = body_by_page.get(page_no, text)

            # 表格区域被排除后，该页可能只剩页眉页脚一句话，
            # 这种碎片不值得单独成块（min_chars 也会挡，但这里先做粗过滤）
            for para in self._split_paragraphs(body):
                doc.blocks.append(
                    Block(type=BlockType.PARAGRAPH, text=para, page=page_no)
                )

            # 该页的表格
            for tbl in tables_by_page.get(page_no, []):
                table_counter += 1
                header_rows, flat_header, data_rows = self._normalize_table(tbl)

                # ## cells 必须保留真实行号（P8 修）
                #
                # 旧写法把表头压进 row=0、数据从 row=1 开始 ——
                # 这**抹平了「表头占几行」的信息**，让下游无法区分
                # 「第 1 行是表头」和「第 1 行是数据」。
                #
                # 实测踩到（股东表）：
                #
                #     r0: 7 个空单元格
                #     r1: 报告期末普通股股东总数 | 13,587 | ...    ← 真正的表头
                #     r3: 股东名称 | 股东性质 | 持股数量 ...        ← 真正的列名
                #
                # 被压成 row0=表头 后，下游拿到的「首行」是 r1，
                # `_looks_like_data_row` 看到 13,587 就判成「无表头」，
                # 整张股东表的列名全丢。
                #
                # ## 表头的每一行都要显式写出（P8 二次踩坑）
                #
                # `flat_header` 是「每列的合并后列名」，只有一份。
                # 如果只写 row=0，而数据从 row=header_rows 开始，
                # 中间那几行就是**空的** —— 下游按「前 header_rows 行」取表头时，
                # 会把第一条数据也算进去：
                #
                #     header_rows=2 → 取 row_ids[:2] = [0, 2]
                #     col0 合并成「股东名称 西藏德锦企业管理有限责任公司」
                #
                # 所以：第 0 行写合并后的列名，其余表头行写占位（空），
                # 只为**占住行号**，让「前 N 行都是表头」这件事成立。
                cells: list[TableCell] = []
                for c, name in enumerate(flat_header):
                    cells.append(TableCell(text=name, row=0, col=c, is_header=True))
                # 占位行：不写内容，只确保 row 1..header_rows-1 存在
                for hr in range(1, header_rows):
                    cells.append(TableCell(text="", row=hr, col=0, is_header=True))

                for r, row in enumerate(data_rows, start=header_rows):
                    for c, val in enumerate(row):
                        if val is None or str(val).strip() == "":
                            continue
                        cells.append(TableCell(text=str(val).strip(), row=r, col=c))

                # 占位列名（「列2」「列3」）只用于兜底，
                # 不能写进 text —— 否则模型会以为「列2」是个真实字段。
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
        """规范表格：判断表头行数，压平字段名，去全空行。

        ## 表头判定为什么不能只看「空格错位」（P7 实测）

        旧逻辑：第一行有空单元格 + 第二行在相同位置补上内容 → 判双行表头。

        实测被它误判的表格（利润表续页）：

            row0: （一）按经营持续性分类-1.持续经营净利润 | 39,998,764.22 | 26,289,237.30
            row1: 2.终止经营净利润（净亏损以“-”号填列）

        这行的 c3+ 是空的（表格宽度由更宽的行决定），
        第一行有空位、第二行 c0 有内容 → 被判成双行表头。
        **结果两条真实数据被当成列名丢掉。**

        ## 新规则：先问「这一行像不像数据」

        表头和数据最靠谱的区分信号是**数字**：
        表头几乎不含纯数字单元格，数据行常常一半是数字。

        1. 第一行若有 ≥1 个纯数字单元格 → **不是表头**，按无表头处理
        2. 否则按旧规则判断是否双行表头，但要求第二行也**不像数据**
        """
        # 去掉整行为空的行
        rows = [r for r in raw if any((c or "").strip() for c in r)]
        if not rows:
            return 0, [], []

        width = max(len(r) for r in rows)
        norm: list[list[str | None]] = [
            list(r) + [None] * (width - len(r)) for r in rows
        ]

        # ## 第一行「含数字」不等于「没有表头」（P8 实测）
        #
        # 上一版规则：首行有任何纯数字单元格 → 判无表头。
        #
        # 被它误判的股东表：
        #
        #     r0: 全空
        #     r1: 报告期末普通股股东总数 | 13,587 | 报告期末表决权恢复的优先股股东总数 | 0
        #     r2: 前10名股东持股情况（不含通过转融通出借股份）
        #     r3: 股东名称 | 股东性质 | 持股数量 | 持股比例(%) | ...
        #
        # r1 带 13,587，判成「数据行」→ 整张表被当成无表头，
        # r3 那行真正的列名降级为数据，股东表彻底不可用。
        #
        # ## 新规则：往后看几行，谁能当列名
        #
        # 真正的列名行有个特征：**短、不含数字、多列都有值**。
        # 所以取前 `LOOKAHEAD` 行里「最像列名」的那行作为表头起点。
        LOOKAHEAD = 4
        header_start = 0
        for i in range(min(LOOKAHEAD, len(norm))):
            if _looks_like_header_row(norm[i], width):
                header_start = i
                break

        # 前 header_start 行是表前的说明性文字（如「前10名股东持股情况」），
        # 丢弃它们 —— 它们既不构成列名，也不是可检索的数据。
        norm = norm[header_start:]

        if _looks_like_data_row(norm[0]):
            return 0, [""] * width, norm

        # 表头行数：第一行必是表头；若第二行与第一行"错位互补"则为双行
        header_n = 1
        if len(norm) >= 2:
            first, second = norm[0], norm[1]
            empty_in_first = sum(1 for c in first if not (c or "").strip())
            filled_in_second = sum(
                1 for i, c in enumerate(second)
                if (c or "").strip() and not (first[i] or "").strip()
            )
            # 第二行也不能像数据，否则说明第一行是表头、第二行就是数据
            if (
                not _looks_like_data_row(second)
                and empty_in_first > 0
                and filled_in_second >= max(1, empty_in_first // 2)
            ):
                header_n = 2

        # ## 去掉「整列全空」的首列（P8 实测）
        #
        # 跨页表格的第二页常有这种情况：最左边一列全是空，
        # 数据从第 2 列开始（合并单元格的残留）。
        #
        #     r0: '' | 王爱英 | '' | 1,978,300 | '' | 人民币普通股 | ''
        #     r1: '' | 宁波沪通私募... | '' | 1,554,326 | ...
        #
        # 切块时首列被当成「行标签」，结果**每行的标题都是空字符串**，
        # 整个块变成
        #     值1: 王爱英 / 值3: 1,978,300 ...
        # 模型拿不到任何行标识。
        #
        # 只处理首列：中间的空列可能是真有语义（如「说明」列留空），
        # 不擅自删除。
        if width > 1 and all(not str(row[0] or "").strip() for row in norm):
            norm = [row[1:] for row in norm]
            width -= 1
            # 同时修正 external 宽度（调用方据此构造 cells 的列号）
            if not width:
                return 0, [], []

        flat: list[str] = []
        for c in range(width):
            parts: list[str] = []
            for r in range(header_n):
                v = norm[r][c]
                s = (v or "").strip()
                if s and (not parts or parts[-1] != s):
                    parts.append(s)
            flat.append("-".join(parts) if parts else "")

        # ## 表头太「薄」时，它大概率是数据（P7 实测）
        #
        # 有一类表格（利润表/资产负债表续页、股东表片段），pdfplumber
        # 会把整张表的第一行硬当成表头。实测这些「表头」长这样：
        #
        #     ['长期借款', '', '']
        #     ['取得子公司及其他营业单位支付的现金净额', '', '']
        #     ['（一）按经营持续性分类', '', '']
        #
        # 只有首列有值、其余全空 —— 这不可能是列名（一列的表没意义），
        # 它就是一条数据。按 0 行表头处理，把这一行还给数据。
        filled_cols = sum(1 for name in flat if name)
        if header_n and filled_cols <= 1 and width >= 2:
            return 0, [""] * width, norm

        return header_n, flat, norm[header_n:]

def _text_outside_boxes(page: Any, boxes: list[tuple[float, float, float, float]]) -> str:
    """提取页面上「不在表格区域」的文字。

    做法：把页面按表格 bbox 的上下边界横向切成若干条带，
    对每条完整落在表格之外的条带调 `page.crop(...).extract_text()`。

    ## 为什么不用 crop 掉每个 bbox 再拼

    `crop` 是取一个矩形区域，多个表格分散在页面上时无法一次排除。
    按 y 轴切条带则可以逐个排除，且不会把表格上下的正文切断 ——
    除非正文行和表格 y 区间重叠（多栏排版才会出现，这里先不处理）。

    ## 失败时返回什么

    任何异常都返回空字符串，由调用方回退到原文 ——
    **宁可正文重复，不能丢正文**。
    """
    if not boxes:
        return ""

    top = float(page.height)
    # 合并重叠的 bbox，避免条带计算出现负高度
    spans: list[tuple[float, float]] = sorted(
        (float(b[1]), float(b[3])) for b in boxes
    )
    merged: list[list[float]] = []
    for y0, y1 in spans:
        if merged and y0 <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], y1)
        else:
            merged.append([y0, y1])

    parts: list[str] = []
    cursor = 0.0
    for y0, y1 in merged:
        if y0 - cursor > 2:  # 小于 2pt 的缝隙没内容，跳过避免无谓 crop
            try:
                band = page.crop((0, cursor, float(page.width), y0))
                got = band.extract_text() or ""
                if got.strip():
                    parts.append(got)
            except Exception:
                pass
        cursor = max(cursor, y1)

    if top - cursor > 2:
        try:
            band = page.crop((0, cursor, float(page.width), top))
            got = band.extract_text() or ""
            if got.strip():
                parts.append(got)
        except Exception:
            pass

    return "\n".join(parts)
