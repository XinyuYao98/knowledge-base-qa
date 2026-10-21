"""切块器：把 ParsedDoc 切成可检索的块。

## 竖切片阶段的简化

完整实现（P4）要做父子块、表格行级切块、heading_path 继承。
这里只做**段落 + 表格**两类块，够验证链路即可。

## 但有一点不能省：表格行必须重复表头

如果把整张表塞进一个块，切块粒度太大，检索时精度差；
如果按行切但不带表头，模型看到的是：

    P-001 | 智能网关 | 1280

它不知道 1280 是单价还是数量。所以每行必须重复表头：

    产品编号 | 名称 | 单价
    P-001 | 智能网关 | 1280
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.rag.parsers.base import Block, BlockType, ParsedDoc


@dataclass(slots=True)
class Chunk:
    """一个可检索的块。"""

    text: str                      # 送去向量化与拼进上下文的文本
    seq: int                       # 文档内顺序
    source: str = ""               # 文件名
    heading_path: str = ""
    page: int | None = None
    sheet: str | None = None
    row: int | None = None
    chunk_type: str = "text"       # text | table_row
    token_estimate: int = 0
    #: 与 PG `chunks.id` 对齐的 ID。
    #:
    #: 为什么需要：Chroma 用 `{doc_id}_{seq}` 作为 id，PG 用 UUID。
    #: 两套 ID 对不上，会导致检索融合后无法回查原文与元数据
    #: （P6 实测踩到：BM25 命中了，_hydrate 却查不到）。
    #: 索引阶段从 PG 回填这个字段，Chroma 就用它当 id。
    chunk_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.token_estimate:
            # 中文粗估：1 字 ≈ 0.7 token；这里用字符数近似，够用于上限控制
            self.token_estimate = max(1, int(len(self.text) * 0.7))

    def citation_label(self) -> str:
        """引用标签，用于拼进上下文：`[1] 文件名 第2页`。"""
        parts = [self.source]
        if self.page is not None:
            parts.append(f"第{self.page}页")
        elif self.sheet:
            parts.append(self.sheet)
        if self.heading_path:
            parts.append(self.heading_path)
        return " ".join(parts)


def _split_long(text: str, limit_chars: int, overlap_chars: int) -> list[str]:
    """超长文本按字符切分，带重叠。

    重叠的意义：避免一个句子被切断后两边都检索不到。
    """
    if len(text) <= limit_chars:
        return [text]
    out: list[str] = []
    start = 0
    step = max(1, limit_chars - overlap_chars)
    while start < len(text):
        out.append(text[start : start + limit_chars])
        if start + limit_chars >= len(text):
            break
        start += step
    return out


def chunk_document(doc: ParsedDoc) -> list[Chunk]:
    """把解析结果切成块。"""
    max_chars = settings.CHUNK_TARGET_TOKENS * 2   # token→字符的粗略换算
    overlap = settings.CHUNK_OVERLAP_TOKENS * 2
    min_chars = settings.MIN_CHUNK_CHARS

    chunks: list[Chunk] = []
    seq = 0

    for block in doc.blocks:
        if block.type == BlockType.TABLE:
            chunks.extend(_chunk_table(block, doc.filename, seq))
            seq = chunks[-1].seq + 1 if chunks else 0
            continue

        text = block.text.strip()
        if len(text) < min_chars:
            continue

        for i, piece in enumerate(_split_long(text, max_chars, overlap)):
            chunks.append(
                Chunk(
                    text=piece,
                    seq=seq,
                    source=doc.filename,
                    heading_path=block.heading_path,
                    page=block.page,
                    sheet=block.sheet,
                    chunk_type="text",
                    extra={"part": i} if i else {},
                )
            )
            seq += 1

    return chunks


def _norm_cell(text: str) -> str:
    """单元格文本归一化：去掉 PDF 换行造成的断词、全角空格。

    PDF 抽出来的单元格常带 `\\n`：
        `'本报告期比\\n上年同期增\\n减变动幅度\\n(%)'`
    不处理的话列头写进块里也是一堆换行，模型照样读不懂。
    """
    if not text:
        return ""
    # `\u3000`（全角空格）在统计表里常用来对齐「地　区」「合　计」
    t = text.replace("\u3000", "").replace("\xa0", " ")
    t = t.replace("\n", "").replace("\r", "")
    return " ".join(t.split())


def _chunk_table(block: Block, source: str, start_seq: int) -> list[Chunk]:
    """表格切块：**一张表一个块**，块内用 cells 重建，列名逐列对齐。

    ## 两个独立的问题，不要混在一起（P8 的重要教训）

    P7 发现表格题大量抖动，当时归因为「列头丢失」。
    P8 动手修时才发现其实是**两个不同的问题**，而且修法互相冲突：

    ### 问题一：文本切分把项目名切断（该修）

    旧实现按 `block.text` 的 `\\n` 分行，而 PDF 的单元格内常有换行：

        归属于上市公司股东的扣除非
        经常性损益的净利润 | 9,392,922.19 | ...

    按行切后，一块的项目名只剩「归属于上市公司股东的扣除非」，
    另一块只剩「经常性损益的净利润」，最坏的一块是「的冲销部分」。

    **修法**：改用 `block.cells` 按 `(row, col)` 重建，与文本里的换行无关。

    ### 问题二：按行切得太碎（**不该修**）

    P8 第一版顺手把每行切成了独立块（一张 46 行的行业表 → 46 个块）。
    结果评测**从 78.9% 掉到 74.3%**。

    实测原因（n07 问第一大股东，候选是）：

        [1] 页眉：上海汇通能源…证券代码：600605
        [2] 封面页眉
        [3] 注：本报告期指本季度初至…
        [4] 厦门驱动力 5.53%（第 3 大股东）
        [5] 香港中央结算 1.19%（第 6 大股东）
            ← 西藏德锦 26.81%（第一大股东）不在候选里

    单行块之间高度相似（46 行行业表每行都是「XX: 数字 / YY: 数字」），
    向量 top20 几乎等于随机抽样；而页眉因为含公司名，反而排最前。
    **答案就在库里，却挤不进 5 条候选。**

    **结论**：切碎解决不了排序问题，只会把「一整张表」的信息密度打散。
    所以：按表成块，块内保留全部行。

    ## 输出形态

        股票代码：600605
        公司简称：汇通能源
        ---
        科目 | 金额
        营业收入 | 30,776,711.66
        营业成本 | 21,882,370.75
        ...

    `列名: 值` 的行内格式仍然保留（消除「数到第几列」），
    但**整张表在同一个块里**，模型能一次看到全部行做比较。

    ## 超长表的处理

    表格超过 `max_chars` 时按行切分，**每块都重复表头** ——
    这时切分是必需的（单块塞不下），不是设计选择。
    """
    # 没有 cells 时退回旧逻辑（Excel/CSV 解析器可能只填 text）
    if not block.cells:
        return _chunk_table_by_text(block, source, start_seq)

    # ---- 1. 还原二维表 ----
    grid: dict[int, dict[int, str]] = {}
    max_col = -1
    for c in block.cells:
        row = int(getattr(c, "row", 0) or 0)
        col = int(getattr(c, "col", 0) or 0)
        grid.setdefault(row, {})[col] = _norm_cell(c.text)
        max_col = max(max_col, col)

    if max_col < 0 or not grid:
        return _chunk_table_by_text(block, source, start_seq)

    n_header = max(0, int(block.header_rows or 0))
    row_ids = sorted(grid)

    # ---- 2. 多行表头逐列合并 ----
    col_names = _merge_header(grid, row_ids[:n_header], max_col)
    has_header = any(col_names)

    # ---- 3. 按「子表」分段 ----
    # 一个 block 里可能装了两张表（股东表就是这样）：
    #   r0-r12 前10名股东（列：股东名称/股东性质/持股数量/...）
    #   r13    新表头（股东名称/持有无限售条件流通股的数量/...）
    # 遇到新表头就断开，否则第二张表会贴上第一张表的列名。
    segments: list[tuple[list[str], list[tuple[int, list[str]]]]] = []
    active_names = list(col_names)
    active_header = has_header
    rows: list[tuple[int, list[str]]] = []

    for r in row_ids[n_header:]:
        cells = grid[r]
        vals = [_norm_cell(cells.get(c, "")) for c in range(max_col + 1)]
        # 整行全空跳过（PDF 抽出的空行很多）
        if not any(v.strip() for v in vals):
            continue

        if _looks_like_subtable_header(vals, active_names):
            # 换表：把已收集的行封成一段
            if rows:
                segments.append((active_names, rows))
            active_names = vals
            active_header = True
            rows = []
            continue

        rows.append((r, vals))

    if rows:
        segments.append((active_names, rows))

    # ---- 4. 每段渲染成一个块（超长才切） ----
    max_chars = settings.CHUNK_TARGET_TOKENS * 2
    out: list[Chunk] = []
    seq = start_seq

    for names, seg_rows in segments:
        header_line = "" if active_header else "（本表无表头）"
        # 先算每行的渲染文本
        rendered = [(_format_row(vals), r) for r, vals in seg_rows]

        buffer: list[str] = []
        first_row_of_block: int | None = None
        for line, r in rendered:
            candidate_len = sum(len(x) + 1 for x in buffer) + len(line)
            if buffer and candidate_len > max_chars:
                out.append(
                    _make_table_chunk(
                        buffer, header_line, names, source, block,
                        seq, first_row_of_block if first_row_of_block is not None else r,
                        active_header,
                    )
                )
                seq += 1
                buffer = []
                first_row_of_block = None
            if first_row_of_block is None:
                first_row_of_block = r
            buffer.append(line)

        if buffer:
            out.append(
                _make_table_chunk(
                    buffer, header_line, names, source, block,
                    seq,
                    first_row_of_block if first_row_of_block is not None else 0,
                    active_header,
                )
            )
            seq += 1

    return out


def _merge_header(
    grid: dict[int, dict[int, str]], header_rows: list[int], max_col: int
) -> list[str]:
    """多行表头逐列合并。

    例：行1「本报告期比」+ 行2「上年同期增减变动幅度」+ 行3「(%)」
      → 「本报告期比上年同期增减变动幅度(%)」
    """
    names: list[str] = []
    for col in range(max_col + 1):
        parts: list[str] = []
        for r in header_rows:
            v = _norm_cell(grid.get(r, {}).get(col, ""))
            # 同一段文字被 PDF 拆到多行时会重复，去重
            if v and (not parts or parts[-1] != v):
                parts.append(v)
        names.append(" ".join(parts).strip())
    return names


def _format_row(vals: list[str]) -> str:
    """把一行渲染成文本：首列作行标签，其余 `列名: 值`。

    首列是「行标签」（项目名、地区名、股东名），不是值。
    输出成 `项目: 营业收入` 会让语义模糊（是在说「项目」这个字段等于营业收入？），
    直接写行标签。

    空值显式写「（空）」，避免模型把缺失当成「没找到这行」。
    """
    head = vals[0] if vals else ""
    if head:
        rest = [f"{v or '（空）'}" for v in vals[1:]]
        return head + (" | " + " | ".join(rest) if rest else "")
    return " | ".join(v or "（空）" for v in vals)


def _make_table_chunk(
    lines: list[str],
    header_line: str,
    names: list[str],
    source: str,
    block: Block,
    seq: int,
    first_row: int,
    has_header: bool,
) -> Chunk:
    """组装一个表格块：表头行 + 数据行 + 列名清单。"""
    parts: list[str] = []
    if header_line:
        parts.append(header_line)

    # 列名清单显式给出，让模型知道每一列是什么
    named = [n for n in names if n]
    if named:
        parts.append("列：" + " / ".join(named))

    parts.extend(lines)
    text = "\n".join(parts)

    return Chunk(
        text=text,
        seq=seq,
        source=source,
        heading_path=block.heading_path,
        page=block.page,
        sheet=block.sheet,
        row=first_row,
        chunk_type="table_row",
        extra={
            "table_index": block.table_index,
            "header": " | ".join(names),
            "row_index": first_row,
            "row_count": len(lines),
            "has_header": has_header,
        },
    )


#: 子表头的单元格最大长度与最短列数
_SUBHEADER_CELL_MAX = 14
_SUBHEADER_MIN_COLS = 2


def _looks_like_subtable_header(
    values: list[str], active_names: list[str]
) -> bool:
    """判断数据区里的一行是不是「新子表的表头」。

    ## 为什么需要这个（P8 实测）

    股东表把两张表塞进了同一个 block。第二张表的表头
    `股东名称 | 持有无限售条件流通股的数量 | 股份种类及数量`
    被当成数据行，贴上第一张表的列名，产出
    `股东名称 西藏德锦...: （空）` 这种错位的块。

    ## 判据：必须与当前列名有**重复的列名**（而不是「看起来像列名」）

    第一版判据是「无数字 + 每格短 + 至少 2 列」，**太宽松**：

        西藏德锦 | 人民币普通股     ← 纯文本的数据行，也被判成子表头

    结果第二张表的第一条股东数据被吃掉。

    新判据：子表头的特征不是「短文本」，而是**同一张表里会重复出现的列名**。
    两张子表通常有共同列（如都叫「股东名称」），所以要求：

        这一行至少有一格，与当前正在用的列名**完全相同**

    自然语言的数据不会恰好等于列名，这个信号比「长度/有无数字」可靠得多。
    """
    filled = [v for v in values if v]
    if len(filled) < _SUBHEADER_MIN_COLS:
        return False
    if any(_is_numeric_cell(v) for v in filled):
        return False

    # **长度检查不能放在最前面直接 return False** ——
    # 首列为空的分支有自己的（更宽的）长度判据，
    # 放在前面会让那个分支永远走不到（P8 踩到）。
    short_all = all(len(v) <= _SUBHEADER_CELL_MAX for v in filled)

    # 关键判据：必须有列名与当前列名重合
    active = {n for n in active_names if n}
    if short_all and any(v in active for v in filled):
        return True

    # ## 首列可能为空（P8 补）
    #
    # 资产负债表首行「本报告期末 | 上年度末 | ...」的首列是空的：
    #
    #     （空） | 本报告期末 | 上年度末
    #
    # 这类行不会与当前列名重合（列名是「项目 / 2026年9月30日 / ...」），
    # 但它确实是新子表的表头。
    #
    # 判据：**除首列外，其余有值的格子都不像数据，且至少 2 个**。
    # 保守起见只在首列为空时启用，避免误伤「项目名 + 类别 + 类别」的数据行。
    if values and not values[0].strip():
        rest = [v for v in values[1:] if v]
        if len(rest) >= _SUBHEADER_MIN_COLS and not any(
            _is_numeric_cell(v) for v in rest
        ):
            # 长度上限比常规表头宽（列名可能很长，如
            # 「本报告期末比上年度末增减变动幅度(%)」有 21 字）。
            # 但**多数格子必须短** —— 数据行的单元格普遍更长。
            short = sum(1 for v in rest if len(v) <= _SUBHEADER_CELL_MAX)
            if short * 2 >= len(rest):
                return True
    return False


def _is_numeric_cell(value: str) -> bool:
    """单元格是否「纯数字」（可带千分位、小数、负号、百分号、括号）。"""
    import re

    return bool(re.match(r"^[-(（]?[-\d,]+(?:\.\d+)?[)）]?%?$", value.strip()))


def _chunk_table_by_text(block: Block, source: str, start_seq: int) -> list[Chunk]:
    """兜底：没有 `cells` 时按文本行切。

    只有这种解析器才会走到这里：它只填了 `text` 没填 `cells`。
    此时无法做列对齐，至少保证每行都带表头。
    """
    lines = [ln for ln in block.text.split("\n") if ln.strip()]
    if len(lines) < 2:
        return []

    header = _norm_cell(lines[0])
    out: list[Chunk] = []
    for i, row in enumerate(lines[1:]):
        out.append(
            Chunk(
                text=f"{header}\n{row}",
                seq=start_seq + i,
                source=source,
                heading_path=block.heading_path,
                page=block.page,
                sheet=block.sheet,
                row=block.row,
                chunk_type="table_row",
                extra={
                    "table_index": block.table_index,
                    "header": header,
                    "row_index": i,
                    "fallback": "no_cells",
                },
            )
        )
    return out
