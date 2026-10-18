"""纯文本解析器（.txt / .log）。

测点：
- 编码识别（GBK/UTF-8/BOM）
- 空文件拒绝
- 按空行分段，保留段落结构
"""

from __future__ import annotations

from pathlib import Path

from app.rag.parsers.base import Block, BlockType, ParsedDoc, ParseError, ParseErrorCode, WarnCode
from app.rag.parsers.registry import BaseParser, guess_encoding


class TxtParser(BaseParser):
    extensions = ("txt", "log", "text")
    format_name = "txt"
    mime = "text/plain"

    def _parse(self, path: Path, doc: ParsedDoc) -> None:
        raw = path.read_bytes()
        encoding, guessed = guess_encoding(raw, path.name)

        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError) as e:
            raise ParseError(
                ParseErrorCode.CORRUPT_FILE,
                f"无法解码文件内容（尝试 {encoding}）",
                detail={"encoding": encoding},
            ) from e

        if guessed:
            doc.warn(
                WarnCode.ENCODING_GUESSED,
                f"文件编码不是 UTF-8，已按 {encoding} 解析",
                encoding=encoding,
            )

        # 统一换行
        text = text.replace("\r\n", "\n").replace("\r", "\n")

        if not text.strip():
            raise ParseError(ParseErrorCode.EMPTY_FILE, "文件内容为空（只有空白字符）")

        doc.meta["encoding"] = encoding

        # 按空行分段；连续空行合并
        paragraphs: list[str] = []
        buf: list[str] = []
        for line in text.split("\n"):
            if line.strip():
                buf.append(line.rstrip())
            elif buf:
                paragraphs.append("\n".join(buf))
                buf = []
        if buf:
            paragraphs.append("\n".join(buf))

        for para in paragraphs:
            doc.blocks.append(Block(type=BlockType.PARAGRAPH, text=para.strip()))

        doc.pages = 1
        doc.page_char_counts = [len(text)]
