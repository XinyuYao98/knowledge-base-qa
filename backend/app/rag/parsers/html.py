"""HTML 解析器（.html / .htm）。

## 真实场景：浏览器「网页另存为」

用户存下来的 HTML 通常长这样：

    page.html
    page_files/
        ├── style.css      （10 个）
        ├── logo.png       （11 个）
        └── attachment.xlsx（9 个下载附件）

所以必须：

1. 剔除 `<script>` / `<style>`（否则 CSS 规则会被当成正文）
2. 剔除导航、页脚、分享按钮等噪声
3. **只取正文容器**（不然「分享到：微信 微博」会进索引）

## 正文容器识别

不同站点的容器 class 不一样。策略：按优先级尝试常见选择器，
都不命中则用「<body> 减去已知噪声标签」兜底。
"""

from __future__ import annotations

import re
from pathlib import Path

from bs4 import BeautifulSoup

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

#: 常见正文容器选择器，按优先级排列
CONTENT_SELECTORS = (
    ".TRS_Editor",          # 中国政府网站（TRS 系统）
    "#UCAP-CONTENT",        # 部分政府站
    ".pages_content",
    ".article-content",
    ".article_content",
    "article",
    ".post-content",
    ".entry-content",
    "#content",
    ".content",
    "main",
)

#: 需要整块删除的噪声标签
NOISE_TAGS = ("script", "style", "nav", "header", "footer", "aside", "noscript", "iframe", "form")

#: 需要删除的噪声 class/id 关键词
NOISE_PATTERNS = re.compile(
    r"(share|social|comment|related|recommend|sidebar|breadcrumb|"
    r"nav|menu|toolbar|advert|banner|copyright|footer|header|"
    r"分享|推荐|相关|评论|导航|版权)",
    re.IGNORECASE,
)


class HtmlParser(BaseParser):
    extensions = ("html", "htm")
    format_name = "html"
    mime = "text/html"

    def _parse(self, path: Path, doc: ParsedDoc) -> None:
        raw = path.read_bytes()
        encoding, guessed = guess_encoding(raw, path.name)
        if guessed:
            doc.warn(WarnCode.ENCODING_GUESSED, f"HTML 编码非 UTF-8，按 {encoding} 解析")

        try:
            html = raw.decode(encoding, errors="replace")
        except Exception as e:
            raise ParseError(ParseErrorCode.CORRUPT_FILE, f"HTML 解码失败：{e}") from e

        soup = BeautifulSoup(html, "lxml")

        # 1. 记录标题
        title = ""
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
        doc.meta["title"] = title

        # 2. 删噪声标签
        removed_tags = 0
        for tag in NOISE_TAGS:
            for el in soup.find_all(tag):
                el.decompose()
                removed_tags += 1

        # 3. 删噪声 class/id 区块
        removed_blocks = 0
        for el in soup.find_all(True):
            if el.attrs is None:
                continue
            marker = " ".join(el.get("class", [])) + " " + str(el.get("id", ""))
            # 只删容器，避免把正文段落误删
            if (
                marker.strip()
                and NOISE_PATTERNS.search(marker)
                and el.name in ("div", "section", "ul", "ol", "nav", "aside")
            ):
                el.decompose()
                removed_blocks += 1

        # 4. 定位正文容器
        container = self._find_content(soup)
        doc.meta["content_selector"] = container.get("data-selector", "") if container else ""

        root = container if container is not None else soup.body or soup

        # 5. 按文档顺序遍历，提取标题 / 段落 / 列表 / 表格
        heading_stack: list[tuple[int, str]] = []
        table_counter = 0

        for el in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "table"]):
            name = el.name

            if name.startswith("h") and len(name) == 2 and name[1].isdigit():
                text = el.get_text(" ", strip=True)
                if not text:
                    continue
                level = int(name[1])
                while heading_stack and heading_stack[-1][0] >= level:
                    heading_stack.pop()
                heading_stack.append((level, text))
                doc.blocks.append(
                    Block(
                        type=BlockType.HEADING,
                        text=text,
                        heading_level=level,
                        heading_path=self._path(heading_stack),
                    )
                )
                continue

            if name == "table":
                table_counter += 1
                blk = self._table_block(el, table_counter, heading_stack)
                if blk is not None:
                    doc.blocks.append(blk)
                continue

            # p / li：跳过表格内的段落（已在表格里处理）
            if el.find_parent("table") is not None:
                continue

            text = el.get_text(" ", strip=True)
            if not text or len(text) < 2:
                continue

            # 过滤明显是导航残留的短句
            if len(text) < 8 and re.fullmatch(r"[\s\W]*", text):
                continue

            doc.blocks.append(
                Block(
                    type=BlockType.LIST_ITEM if name == "li" else BlockType.PARAGRAPH,
                    text=text,
                    heading_path=self._path(heading_stack),
                )
            )

        if removed_tags or removed_blocks:
            doc.meta["removed_noise"] = {"tags": removed_tags, "blocks": removed_blocks}

        if not doc.blocks:
            raise ParseError(ParseErrorCode.EMPTY_FILE, "HTML 中没有可提取的正文")

        doc.pages = None
        doc.page_char_counts = []
        doc.meta["table_count"] = table_counter
        doc.meta["encoding"] = encoding

    # ------------------------------------------------------------------
    @staticmethod
    def _find_content(soup: BeautifulSoup):  # type: ignore[no-untyped-def]
        """按优先级找正文容器。找到后打上标记便于记录。"""
        for sel in CONTENT_SELECTORS:
            el = soup.select_one(sel)
            if el is not None:
                text_len = len(el.get_text(strip=True))
                # 太短说明不是正文
                if text_len >= 100:
                    el["data-selector"] = sel
                    return el
        return None

    @staticmethod
    def _table_block(el, index: int, heading_stack: list[tuple[int, str]]) -> Block | None:  # type: ignore[no-untyped-def]
        rows: list[list[str]] = []
        for tr in el.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
            if any(cells):
                rows.append(cells)

        if not rows:
            return None

        width = max(len(r) for r in rows)
        norm = [r + [""] * (width - len(r)) for r in rows]
        header = norm[0]
        flat = [h if h else f"列{i + 1}" for i, h in enumerate(header)]

        table_cells: list[TableCell] = []
        for c, name in enumerate(flat):
            table_cells.append(TableCell(text=name, row=0, col=c, is_header=True))
        for r, row in enumerate(norm[1:], start=1):
            for c, val in enumerate(row):
                if val:
                    table_cells.append(TableCell(text=val, row=r, col=c))

        lines = [" | ".join(flat)]
        for row in norm[1:]:
            lines.append(" | ".join(row))

        return Block(
            type=BlockType.TABLE,
            text="\n".join(lines),
            heading_path=" > ".join(t for _, t in heading_stack),
            table_index=index,
            cells=table_cells,
            header_rows=1,
            extra={"row_count": len(norm) - 1, "col_count": width},
        )

    @staticmethod
    def _path(stack: list[tuple[int, str]]) -> str:
        return " > ".join(t for _, t in stack)
