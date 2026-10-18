"""解析层的统一数据契约。

**这一层是整个项目的地基。** 上层（切块、检索、引用）只认这里的类型，
不关心原始格式是什么。改格式支持时，只需要加一个 Parser，上层不动。

## 三个核心类型

- `Block`：一个语义块（段落 / 标题 / 表格 / 列表项 / 图片说明）
- `ParsedDoc`：一份文档的完整解析结果
- `ParseWarning`：解析过程中的降级告警（不阻断，但必须让用户看见）

## 设计要点

1. **保留结构，不做纯文本拼接**
   标题层级、表格行列、页码都要留下。丢掉它们，后面就没法做「引用落到第几页第几行」。

2. **警告不吞掉**
   "这页是扫描件""这个单元格是公式但 Excel 没缓存值" —— 这些必须上报。
   用户看到「答不出来」，得能查到是因为解析丢了内容。

3. **失败要分类**
   `no_text_layer`（扫描件）和 `corrupt_file`（文件坏了）在用户眼里是两回事。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class BlockType(StrEnum):
    """块类型。决定后续怎么切、怎么引用。"""

    HEADING = "heading"       # 标题，带 level（1-6）
    PARAGRAPH = "paragraph"   # 正文段落
    LIST_ITEM = "list_item"   # 列表项
    TABLE = "table"           # 表格（整体一个块，内部保留行列）
    TABLE_ROW = "table_row"   # 表格行（行级切块后的产物）
    NOTE = "note"             # 备注（PPT notes_slide、Word 批注）
    CAPTION = "caption"       # 图注 / 表注
    CODE = "code"             # 代码块
    OTHER = "other"


class WarnCode(StrEnum):
    """告警码。前端按这个码展示不同的提示文案。"""

    NO_TEXT_LAYER = "no_text_layer"          # 整页无可提取文字（疑似扫描件）
    PARTIAL_TEXT = "partial_text"            # 部分页无文字
    FORMULA_NO_CACHE = "formula_no_cache"    # Excel 公式无缓存值
    MERGED_CELL = "merged_cell"              # 合并单元格已展开
    IMAGE_SKIPPED = "image_skipped"          # 图片内容未提取（无 OCR）
    ENCODING_GUESSED = "encoding_guessed"    # 编码靠猜
    TRUNCATED = "truncated"                  # 内容超长被截断
    TABLE_DETECT_FAILED = "table_detect_failed"  # 表格识别失败
    EMPTY_SECTION = "empty_section"          # 某节无内容
    GENERIC = "generic"


class ParseErrorCode(StrEnum):
    """致命错误码。抛出 ParseError 时用它区分原因。"""

    EMPTY_FILE = "empty_file"
    NO_TEXT_LAYER = "no_text_layer"
    UNSUPPORTED_FORMAT = "unsupported_format"
    ENCRYPTED = "encrypted"
    CORRUPT_FILE = "corrupt_file"
    TOO_LARGE = "too_large"
    LEGACY_FORMAT = "legacy_format"          # .doc/.xls/.ppt


class ParseError(Exception):
    """解析失败。携带错误码，便于上层区分处理。

    放在 base.py 而不是 registry.py：解析器需要同时导入 base 与 registry，
    把异常定义在 base 可以避免 registry → base → registry 的循环依赖。
    """

    def __init__(self, code: ParseErrorCode, message: str, *, detail: dict | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": str(self.code), "message": self.message, "detail": self.detail}


@dataclass(slots=True)
class ParseWarning:
    """可恢复的问题。不阻断解析，但必须记录并展示。"""

    code: WarnCode
    message: str
    page: int | None = None
    location: str | None = None      # 例如 "Sheet1!D12"
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": str(self.code), "message": self.message}
        if self.page is not None:
            out["page"] = self.page
        if self.location:
            out["location"] = self.location
        if self.detail:
            out["detail"] = self.detail
        return out


@dataclass(slots=True)
class TableCell:
    """表格单元格。

    rowspan/colspan 保留下来，是为了让引用能说清
    "这个数字属于合并了三行的那个区域"，而不是丢失合并关系。
    """

    text: str
    row: int
    col: int
    rowspan: int = 1
    colspan: int = 1
    is_header: bool = False


@dataclass(slots=True)
class Block:
    """一个语义块。"""

    type: BlockType
    text: str
    seq: int = 0

    # 结构信息
    heading_level: int | None = None       # HEADING 专用，1-6
    heading_path: str = ""                 # 例如 "第三章 > 3.2 差旅 > 住宿标准"

    # 定位信息（引用要落到这里）
    page: int | None = None                # PDF 页码 / PPT 幻灯片序号
    page_end: int | None = None            # 跨页块
    sheet: str | None = None               # Excel 工作表名
    row: int | None = None                 # Excel 行号 / 表格行号
    col_span: str | None = None            # 例如 "A:D"

    # 表格专用
    table_index: int | None = None         # 文档内第几张表
    cells: list[TableCell] = field(default_factory=list)
    header_rows: int = 0

    # 元数据
    char_count: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.char_count:
            self.char_count = len(self.text)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "type": str(self.type),
            "seq": self.seq,
            "text": self.text,
            "char_count": self.char_count,
        }
        for key in (
            "heading_level",
            "page",
            "page_end",
            "sheet",
            "row",
            "table_index",
        ):
            val = getattr(self, key)
            if val is not None:
                out[key] = val
        if self.heading_path:
            out["heading_path"] = self.heading_path
        if self.col_span:
            out["col_span"] = self.col_span
        if self.header_rows:
            out["header_rows"] = self.header_rows
        if self.cells:
            out["cells"] = [
                {
                    "text": c.text,
                    "row": c.row,
                    "col": c.col,
                    "rowspan": c.rowspan,
                    "colspan": c.colspan,
                    "is_header": c.is_header,
                }
                for c in self.cells
            ]
        if self.extra:
            out["extra"] = self.extra
        return out


@dataclass(slots=True)
class ParsedDoc:
    """一份文档的解析结果。"""

    # 基本信息
    filename: str
    format: str                                # pdf / docx / xlsx / ...
    mime: str = ""

    # 内容
    blocks: list[Block] = field(default_factory=list)
    pages: int | None = None                   # 页数 / 幻灯片数 / 工作表数

    # 质量信号
    warnings: list[ParseWarning] = field(default_factory=list)
    # 每页可提取字符数。扫描件检测的依据：min < 阈值 → no_text_layer
    page_char_counts: list[int] = field(default_factory=list)

    # 额外元信息（文档属性、表格数量统计等）
    meta: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # 派生属性
    # ------------------------------------------------------------------
    @property
    def total_chars(self) -> int:
        return sum(b.char_count for b in self.blocks)

    @property
    def block_count(self) -> int:
        return len(self.blocks)

    @property
    def table_count(self) -> int:
        return sum(1 for b in self.blocks if b.type == BlockType.TABLE)

    @property
    def heading_count(self) -> int:
        return sum(1 for b in self.blocks if b.type == BlockType.HEADING)

    def warn(
        self,
        code: WarnCode,
        message: str,
        *,
        page: int | None = None,
        location: str | None = None,
        **detail: Any,
    ) -> None:
        self.warnings.append(
            ParseWarning(code=code, message=message, page=page, location=location, detail=detail)
        )

    def text_only(self) -> str:
        """拼接全部块的纯文本。仅用于调试与长度估算，不用于检索。"""
        return "\n".join(b.text for b in self.blocks if b.text)

    def to_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "format": self.format,
            "mime": self.mime,
            "pages": self.pages,
            "block_count": self.block_count,
            "table_count": self.table_count,
            "total_chars": self.total_chars,
            "page_char_counts": self.page_char_counts,
            "warnings": [w.to_dict() for w in self.warnings],
            "meta": self.meta,
            "blocks": [b.to_dict() for b in self.blocks],
        }
