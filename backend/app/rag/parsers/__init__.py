"""解析层。

对外只需记住两个名字：

    from app.rag.parsers import parse_file, ParseError
"""

from app.rag.parsers.base import (
    Block,
    BlockType,
    ParsedDoc,
    ParseError,
    ParseErrorCode,
    ParseWarning,
    TableCell,
    WarnCode,
)
from app.rag.parsers.dispatcher import (
    SUPPORTED_EXTENSIONS,
    get_parser,
    parse_file,
    supported_mime_types,
)

__all__ = [
    "SUPPORTED_EXTENSIONS",
    "Block",
    "BlockType",
    "ParseError",
    "ParseErrorCode",
    "ParseWarning",
    "ParsedDoc",
    "TableCell",
    "WarnCode",
    "get_parser",
    "parse_file",
    "supported_mime_types",
]
