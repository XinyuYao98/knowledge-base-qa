"""解析器基类与公共工具。

所有格式解析器继承 `BaseParser`，实现 `parse()`。
基类负责：文件校验、异常包装、块序号编排、编码回退等重复劳动。
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from pathlib import Path

from app.core.config import settings
from app.core.logging import get_logger
from app.rag.parsers.base import (
    ParsedDoc,
    ParseError,
    ParseErrorCode,
    WarnCode,
)

log = get_logger("parser")

__all__ = ["BaseParser", "ParseError", "ParseErrorCode", "guess_encoding"]


class BaseParser(ABC):
    """解析器基类。"""

    #: 支持的扩展名（小写，不含点）
    extensions: tuple[str, ...] = ()
    #: 格式标识，写入 ParsedDoc.format
    format_name: str = ""
    #: 对应 MIME
    mime: str = "application/octet-stream"

    def parse(self, path: Path) -> ParsedDoc:
        """解析入口。子类实现 `_parse`。"""
        self._validate(path)
        doc = ParsedDoc(filename=path.name, format=self.format_name, mime=self.mime)

        try:
            self._parse(path, doc)
        except ParseError:
            raise
        except Exception as e:
            log.exception("parse_failed", filename=path.name, parser=self.format_name)
            raise ParseError(
                ParseErrorCode.CORRUPT_FILE,
                f"文件无法解析：{type(e).__name__}",
                detail={"error": str(e)[:200]},
            ) from e

        self._finalize(doc, path)
        return doc

    # ------------------------------------------------------------------
    # 子类实现
    # ------------------------------------------------------------------
    @abstractmethod
    def _parse(self, path: Path, doc: ParsedDoc) -> None:
        """把内容写进 doc.blocks。失败时抛 ParseError。"""

    # ------------------------------------------------------------------
    # 公共逻辑
    # ------------------------------------------------------------------
    def _validate(self, path: Path) -> None:
        if not path.exists():
            raise ParseError(ParseErrorCode.CORRUPT_FILE, f"文件不存在：{path}")

        size = path.stat().st_size
        if size == 0:
            raise ParseError(ParseErrorCode.EMPTY_FILE, "文件为空（0 字节）")

        if size > settings.MAX_SINGLE_FILE_BYTES:
            raise ParseError(
                ParseErrorCode.TOO_LARGE,
                f"文件超过 {settings.MAX_SINGLE_FILE_BYTES // 1024 // 1024}MB 上限",
                detail={"size_bytes": size},
            )

    def _finalize(self, doc: ParsedDoc, path: Path) -> None:
        """补齐 seq、统计字符数、扫描件判定。"""
        for i, block in enumerate(doc.blocks):
            block.seq = i

        # 扫描件判定：整份文档提取到的字符太少
        if doc.total_chars == 0:
            raise ParseError(
                ParseErrorCode.NO_TEXT_LAYER,
                "文档中没有可提取的文字。若为扫描件或图片，本系统暂不支持 OCR。",
                detail={"pages": doc.pages},
            )

        # 部分页是扫描件
        if doc.page_char_counts:
            empty_pages = [i + 1 for i, n in enumerate(doc.page_char_counts)
                           if n < settings.PDF_SCANNED_TEXT_PER_PAGE]
            if empty_pages and len(empty_pages) < len(doc.page_char_counts):
                doc.warn(
                    WarnCode.PARTIAL_TEXT,
                    f"第 {', '.join(map(str, empty_pages[:5]))} 页没有可提取文字，"
                    f"这部分内容可能缺失",
                    pages=empty_pages,
                )
            elif empty_pages:
                raise ParseError(
                    ParseErrorCode.NO_TEXT_LAYER,
                    "全部页面都没有可提取文字，疑似扫描件。本系统暂不支持 OCR。",
                    detail={"pages": doc.pages},
                )

        doc.meta["sha256"] = self._file_sha256(path)
        doc.meta["size_bytes"] = path.stat().st_size

    @staticmethod
    def _file_sha256(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()


def guess_encoding(raw: bytes, filename: str = "") -> tuple[str, bool]:
    """猜编码。返回 (编码名, 是否靠猜测)。

    顺序很关键：
    1. BOM 优先（最可靠）
    2. UTF-8 严格解码（能成功就一定是 UTF-8）
    3. charset-normalizer 判断（GBK/GB18030 等）
    4. 兜底 GB18030（中文 Windows 最常见）

    **不要先试 GBK**：很多 UTF-8 字节串在 GBK 下也能解出乱码而不报错。
    """
    # 1. BOM
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig", False
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16", False

    # 2. UTF-8 严格
    try:
        raw.decode("utf-8")
        return "utf-8", False
    except UnicodeDecodeError:
        pass

    # 3. charset-normalizer
    try:
        import charset_normalizer

        best = charset_normalizer.from_bytes(raw).best()
        if best and best.encoding:
            enc = best.encoding.lower()
            # gb2312/gbk 都归到 gb18030（超集，不会更差）
            if enc in ("gb2312", "gbk", "gb18030", "hz"):
                return "gb18030", True
            return enc, True
    except Exception:
        pass

    # 4. 兜底
    return "gb18030", True
