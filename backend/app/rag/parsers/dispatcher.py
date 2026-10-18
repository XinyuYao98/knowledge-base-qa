"""解析器调度：按扩展名选择解析器。

上层只调 `parse_file(path)`，不关心格式。

## 格式支持矩阵

| 扩展名 | 解析器 | 说明 |
|---|---|---|
| .pdf | PdfParser | pypdf 抽正文 + pdfplumber 抽表格 |
| .docx | DocxParser | |
| .doc | LegacyDocParser | 明确拒绝，提示另存 |
| .xlsx/.xlsm | XlsxParser | 合并单元格展开 + 双行表头压平 |
| .xls | LegacyXlsParser | 明确拒绝 |
| .pptx | PptxParser | 含 notes_slide |
| .ppt | LegacyPptParser | 明确拒绝 |
| .csv/.tsv | CsvParser | |
| .md/.markdown | MarkdownParser | |
| .txt/.log | TxtParser | |
| .html/.htm | HtmlParser | 正文容器识别 + 噪声剔除 |
"""

from __future__ import annotations

from pathlib import Path

from app.rag.parsers.base import ParsedDoc, ParseError, ParseErrorCode
from app.rag.parsers.csv import CsvParser
from app.rag.parsers.docx import DocxParser, LegacyDocParser
from app.rag.parsers.html import HtmlParser
from app.rag.parsers.md import MarkdownParser
from app.rag.parsers.pdf import PdfParser
from app.rag.parsers.pptx import PptxParser
from app.rag.parsers.registry import BaseParser
from app.rag.parsers.txt import TxtParser
from app.rag.parsers.xlsx import XlsxParser


class _LegacyOfficeParser(BaseParser):
    """老格式 Excel / PPT 的统一拒绝器。"""

    def __init__(self, exts: tuple[str, ...], fmt: str, hint: str) -> None:
        self.extensions = exts
        self.format_name = fmt
        self._hint = hint

    def _parse(self, path: Path, doc: ParsedDoc) -> None:  # pragma: no cover
        raise ParseError(ParseErrorCode.LEGACY_FORMAT, self._hint)


#: 全部解析器实例
_PARSERS: tuple[BaseParser, ...] = (
    PdfParser(),
    DocxParser(),
    LegacyDocParser(),
    XlsxParser(),
    _LegacyOfficeParser(
        ("xls",),
        "xls",
        "不支持 .xls 老格式。请用 Excel 或 WPS 另存为 .xlsx 后重新上传。",
    ),
    PptxParser(),
    _LegacyOfficeParser(
        ("ppt",),
        "ppt",
        "不支持 .ppt 老格式。请用 PowerPoint 或 WPS 另存为 .pptx 后重新上传。",
    ),
    CsvParser(),
    MarkdownParser(),
    TxtParser(),
    HtmlParser(),
)

#: 扩展名 → 解析器
_REGISTRY: dict[str, BaseParser] = {
    ext: parser for parser in _PARSERS for ext in parser.extensions
}

#: 对外声明的支持列表（用于前端与文档）
SUPPORTED_EXTENSIONS: tuple[str, ...] = tuple(sorted(_REGISTRY))


def get_parser(filename: str) -> BaseParser:
    """按文件名取解析器。未支持时抛 ParseError。"""
    ext = Path(filename).suffix.lower().lstrip(".")
    parser = _REGISTRY.get(ext)
    if parser is None:
        raise ParseError(
            ParseErrorCode.UNSUPPORTED_FORMAT,
            f"不支持的文件格式：.{ext or '(无扩展名)'}。"
            f"支持：{', '.join('.' + e for e in SUPPORTED_EXTENSIONS)}",
            detail={"ext": ext, "supported": list(SUPPORTED_EXTENSIONS)},
        )
    return parser


def parse_file(path: Path | str) -> ParsedDoc:
    """解析单个文件。这是解析层的唯一对外入口。"""
    p = Path(path)
    parser = get_parser(p.name)
    return parser.parse(p)


def supported_mime_types() -> dict[str, str]:
    """扩展名 → MIME，供上传接口校验使用。"""
    return {ext: parser.mime for ext, parser in _REGISTRY.items()}
