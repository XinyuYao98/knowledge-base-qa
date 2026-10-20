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


def _chunk_table(block: Block, source: str, start_seq: int) -> list[Chunk]:
    """表格按行切块，每块带表头。"""
    lines = [ln for ln in block.text.split("\n") if ln.strip()]
    if len(lines) < 2:
        return []

    header = lines[0]
    rows = lines[1:]
    out: list[Chunk] = []

    for i, row in enumerate(rows):
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
                },
            )
        )
    return out
