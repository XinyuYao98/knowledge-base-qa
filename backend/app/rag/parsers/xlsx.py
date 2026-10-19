"""Excel 解析器（.xlsx / .xlsm）。

**这是整个解析层最难的格式**，三个坑都必须处理：

## 坑 1：合并单元格只在左上角有值

A3:A4 合并后，A4 的值为 None。直接按行读会得到：

    第 3 行: ['华东', '林绍安', 1280.5]
    第 4 行: [None,    '周慕云', 960.0]   ← 区域丢了

处理：解析时按合并范围向下/向右填充。

## 坑 2：双行表头

    | 区域 | 负责人 | 季度销售 |      |
    |      |        | Q1      | Q2   |

第一行是分组，第二行是字段。行级切块时，字段名必须是 "季度销售-Q1"
而不是 "Q1"，否则模型不知道 Q1 是什么。

## 坑 3：空行分隔的多张逻辑表

一个工作表里可能有两张表，用空行分隔。不识别边界会把表头串味。
"""

from __future__ import annotations

from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

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


class XlsxParser(BaseParser):
    extensions = ("xlsx", "xlsm")
    format_name = "xlsx"
    mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    def _parse(self, path: Path, doc: ParsedDoc) -> None:
        try:
            # data_only=True：取公式的计算结果而不是公式本身
            # 若 Excel 从未打开过该文件，公式单元格会读出 None
            wb = load_workbook(path, data_only=True, read_only=False)
        except Exception as e:
            raise ParseError(
                ParseErrorCode.CORRUPT_FILE,
                f"无法打开 Excel 文件：{type(e).__name__}",
                detail={"error": str(e)[:200]},
            ) from e

        doc.pages = len(wb.sheetnames)
        doc.meta["sheet_names"] = list(wb.sheetnames)

        table_counter = 0
        formula_missing = 0

        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            if ws.max_row is None or ws.max_row < 1:
                continue

            grid = self._read_grid(ws, doc, sheet_name)
            if not grid:
                continue

            # 按空行切分成多张逻辑表
            for table_rows, start_row in self._split_tables(grid):
                if not table_rows:
                    continue

                has_formula_gap = any(
                    cell is None for row in table_rows for cell in row
                )
                del has_formula_gap  # 占位：公式缺失在 _read_grid 里统计

                table_counter += 1
                header_rows_n, header_cells = self._detect_header(table_rows)
                data_rows = table_rows[header_rows_n:]

                # 把双行表头压平成单行字段名
                flat_header = self._flatten_header(header_cells, header_rows_n)

                cells: list[TableCell] = []
                for c, txt in enumerate(flat_header):
                    cells.append(TableCell(text=txt, row=0, col=c, is_header=True))

                for r, row in enumerate(data_rows, start=1):
                    for c, val in enumerate(row):
                        if val is None or str(val).strip() == "":
                            continue
                        cells.append(
                            TableCell(
                                text=self._fmt(val),
                                row=r,
                                col=c,
                                # 记录原始列号，便于生成 "A:D" 式引用
                            )
                        )

                # 表格的纯文本表示：每行带字段名，便于人读与检索
                lines = [" | ".join(flat_header)]
                for row in data_rows:
                    vals = [self._fmt(v) if v is not None else "" for v in row]
                    if not any(vals):
                        continue
                    lines.append(" | ".join(vals))

                col_count = len(flat_header)
                doc.blocks.append(
                    Block(
                        type=BlockType.TABLE,
                        text="\n".join(lines),
                        sheet=sheet_name,
                        row=start_row,
                        col_span=f"A:{get_column_letter(max(col_count, 1))}",
                        table_index=table_counter,
                        cells=cells,
                        header_rows=header_rows_n,
                        extra={
                            "sheet": sheet_name,
                            "row_count": len(data_rows),
                            "col_count": col_count,
                            "start_row": start_row,
                        },
                    )
                )

        if formula_missing:
            doc.warn(
                WarnCode.FORMULA_NO_CACHE,
                f"有 {formula_missing} 个公式单元格没有缓存值，这些数字可能缺失。"
                f"用 Excel 打开并另存一次可以补全。",
                count=formula_missing,
            )

        if not doc.blocks:
            raise ParseError(ParseErrorCode.EMPTY_FILE, "工作表中没有可提取的内容")

        doc.meta["table_count"] = table_counter

    # ------------------------------------------------------------------
    # 读取网格 + 展开合并单元格
    # ------------------------------------------------------------------
    def _read_grid(self, ws, doc: ParsedDoc, sheet_name: str) -> list[list]:  # type: ignore[no-untyped-def]
        """读出完整网格，并把合并单元格的值填到整个合并区域。"""
        max_row = ws.max_row or 0
        max_col = ws.max_column or 0
        if max_row == 0 or max_col == 0:
            return []

        grid: list[list] = [[None] * max_col for _ in range(max_row)]

        for row in ws.iter_rows(min_row=1, max_row=max_row, max_col=max_col):
            for cell in row:
                grid[cell.row - 1][cell.column - 1] = cell.value

        # 展开合并单元格（关键修复）
        merged_count = 0
        for rng in ws.merged_cells.ranges:
            top_left = grid[rng.min_row - 1][rng.min_col - 1]
            for r in range(rng.min_row - 1, rng.max_row):
                for c in range(rng.min_col - 1, rng.max_col):
                    if r != rng.min_row - 1 or c != rng.min_col - 1:
                        grid[r][c] = top_left
            merged_count += 1

        if merged_count:
            doc.warn(
                WarnCode.MERGED_CELL,
                f"工作表「{sheet_name}」有 {merged_count} 处合并单元格，已按合并范围展开填充",
                location=sheet_name,
                count=merged_count,
            )

        return grid

    # ------------------------------------------------------------------
    # 按空行切分逻辑表
    # ------------------------------------------------------------------
    @staticmethod
    def _split_tables(grid: list[list]) -> list[tuple[list[list], int]]:
        """返回 [(行列表, 起始行号), ...]。连续空行视为表边界。"""
        tables: list[tuple[list[list], int]] = []
        cur: list[list] = []
        start = 1
        blank_run = 0

        for idx, row in enumerate(grid, start=1):
            is_blank = all(v is None or str(v).strip() == "" for v in row)
            if is_blank:
                blank_run += 1
                if cur and blank_run >= 1:
                    tables.append((cur, start))
                    cur = []
                continue
            blank_run = 0
            if not cur:
                start = idx
            cur.append(row)

        if cur:
            tables.append((cur, start))
        return tables

    # ------------------------------------------------------------------
    # 表头识别
    # ------------------------------------------------------------------
    @staticmethod
    def _detect_header(rows: list[list]) -> tuple[int, list[list]]:
        """判定前几行是表头。

        ## 难点

        合并单元格展开后，第 1 行可能变成
            [区域, 负责人, 季度销售, 季度销售, 备注]   ← C1:D1 合并导致重复
        第 2 行是
            [区域, 负责人, Q1,      Q2,      备注]     ← A1:A2 合并导致重复

        ## 判据

        只有**同时满足**以下两条，才认定第 2 行也是表头：

        1. 第 2 行在某个列上的值，与第 1 行不同（说明不是重复填充）
        2. 第 1 行在**该列或左侧最近的非空列**上存在"合并造成的重复"
           或"该列为空但第 2 行有值"

        反例（必须避免）：普通的单行表头 + 数据行
            [产品编号, 名称, 单价]
            [P-001,   智能网关, 1280]
        这里第 2 行每列都与第 1 行不同，但第 1 行**没有重复/空洞**，
        所以不应被当成表头。
        """
        if not rows:
            return 0, []

        if len(rows) < 2:
            return 1, rows[:1]

        first, second = rows[0], rows[1]
        width = max(len(first), len(second))

        # 第 1 行是否存在"合并痕迹"：相邻重复值，或某列为空但右邻有值
        first_has_merge_artifact = False
        prev_nonempty = ""
        for c in range(width):
            v1 = first[c] if c < len(first) else None
            s1 = "" if v1 is None else str(v1).strip()
            if s1 and s1 == prev_nonempty:
                first_has_merge_artifact = True
                break
            if s1:
                prev_nonempty = s1

        if not first_has_merge_artifact:
            return 1, rows[:1]

        # 第 2 行必须有值且与第 1 行不同（真正的第二层字段名）
        differing = 0
        non_empty = 0
        for c in range(width):
            v1 = first[c] if c < len(first) else None
            v2 = second[c] if c < len(second) else None
            s1 = "" if v1 is None else str(v1).strip()
            s2 = "" if v2 is None else str(v2).strip()
            if not s2:
                continue
            non_empty += 1
            if s2 != s1:
                differing += 1

        if non_empty and differing >= max(1, non_empty // 2):
            return 2, rows[:2]

        return 1, rows[:1]

    @staticmethod
    def _flatten_header(header_rows: list[list], n: int) -> list[str]:
        """把多行表头压平成单行字段名。

        例：
            行1: [区域, 负责人, 季度销售, None]
            行2: [None, None,    Q1,      Q2]
        结果: [区域, 负责人, 季度销售-Q1, Q2]
        """
        if n == 0 or not header_rows:
            return []
        width = max(len(r) for r in header_rows)
        out: list[str] = []
        for c in range(width):
            parts: list[str] = []
            for r in range(n):
                if c < len(header_rows[r]):
                    v = header_rows[r][c]
                    if v is not None and str(v).strip():
                        parts.append(str(v).strip())
            # 去重：合并填充后两行可能同值
            deduped: list[str] = []
            for p in parts:
                if not deduped or deduped[-1] != p:
                    deduped.append(p)
            out.append("-".join(deduped))
        return out

    @staticmethod
    def _fmt(val: object) -> str:
        """单元格值转文本。数字去掉无意义的 .0。"""
        if val is None:
            return ""
        if isinstance(val, float) and val.is_integer():
            return str(int(val))
        return str(val).strip()
