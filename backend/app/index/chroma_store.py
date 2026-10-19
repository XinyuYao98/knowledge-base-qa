"""Chroma 存储封装。

## 多租户隔离（D3 决策）

每个知识库一个 collection，命名 `kb_{kb_id}`。

**不按用户分 collection** 的理由：
- 一个用户可以有多个库，按用户分会迫使检索时再 filter，反而更慢
- collection 名带 kb_id，物理隔离，漏写 filter 也不会串库

## 距离度量

用 cosine。Chroma 的 `hnsw:space=cosine` 下，
`distance = 1 - cosine_similarity`，所以 `similarity = 1 - distance`。

**注意**：Chroma 返回的 distance 越小越相似，别搞反。
"""

from __future__ import annotations

import contextlib
import uuid
from typing import Any

import chromadb
from chromadb.config import Settings as ChromaSettings

from app.core.config import settings
from app.core.logging import get_logger
from app.rag.chunker import Chunk

log = get_logger("chroma")

_client: chromadb.api.ClientAPI | None = None


def get_client() -> chromadb.api.ClientAPI:
    """按配置返回 Chroma 客户端。

    - embedded：进程内持久化。方便，但多进程（api + worker）看不到彼此的写入
    - http：连独立服务。生产必须用这个
    """
    global _client
    if _client is not None:
        return _client

    if settings.CHROMA_MODE == "http":
        _client = chromadb.HttpClient(
            host=settings.CHROMA_HOST,
            port=settings.CHROMA_PORT,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
    else:
        _client = chromadb.PersistentClient(
            path=settings.CHROMA_PATH,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
    return _client


def collection_name(kb_id: str | uuid.UUID) -> str:
    return f"kb_{str(kb_id).replace('-', '')}"


def get_or_create_collection(kb_id: str | uuid.UUID) -> Any:
    return get_client().get_or_create_collection(
        name=collection_name(kb_id),
        metadata={"hnsw:space": "cosine"},
    )


def add_chunks(
    kb_id: str | uuid.UUID,
    document_id: str | uuid.UUID,
    chunks: list[Chunk],
    vectors: list[list[float]],
) -> int:
    """写入向量。返回写入条数。

    id 用 `{doc_id}_{seq}`，保证幂等：同一文档重复写入会覆盖而非重复。
    """
    if not chunks:
        return 0
    if len(chunks) != len(vectors):
        raise ValueError(f"块数 {len(chunks)} 与向量数 {len(vectors)} 不一致")

    col = get_or_create_collection(kb_id)
    col.upsert(
        ids=[f"{document_id}_{c.seq}" for c in chunks],
        embeddings=vectors,
        documents=[c.text for c in chunks],
        metadatas=[
            {
                "document_id": str(document_id),
                "seq": c.seq,
                "source": c.source,
                "heading_path": c.heading_path or "",
                "page": c.page if c.page is not None else -1,
                "sheet": c.sheet or "",
                "chunk_type": c.chunk_type,
                "row": c.row if c.row is not None else -1,
            }
            for c in chunks
        ],
    )
    log.info(
        "chroma_add",
        kb_id=str(kb_id),
        document_id=str(document_id),
        count=len(chunks),
    )
    return len(chunks)


def query(
    kb_id: str | uuid.UUID,
    vector: list[float],
    top_k: int = 20,
) -> list[dict[str, Any]]:
    """向量检索。返回按相似度降序的列表。"""
    try:
        col = get_or_create_collection(kb_id)
    except Exception as e:
        log.warning("chroma_collection_unavailable", kb_id=str(kb_id), err=type(e).__name__)
        return []

    try:
        res = col.query(query_embeddings=[vector], n_results=top_k)
    except Exception as e:
        # 空 collection 会让某些版本报错，视为无结果
        log.warning("chroma_query_failed", kb_id=str(kb_id), err=type(e).__name__)
        return []

    ids = (res.get("ids") or [[]])[0]
    docs = (res.get("documents") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]

    out: list[dict[str, Any]] = []
    for i, cid in enumerate(ids):
        distance = dists[i] if i < len(dists) else 1.0
        out.append(
            {
                "id": cid,
                "text": docs[i] if i < len(docs) else "",
                "metadata": metas[i] if i < len(metas) else {},
                # cosine 距离 → 相似度
                "score": 1.0 - float(distance),
                "distance": float(distance),
            }
        )
    return out


def delete_document(kb_id: str | uuid.UUID, document_id: str | uuid.UUID) -> None:
    try:
        col = get_or_create_collection(kb_id)
        col.delete(where={"document_id": str(document_id)})
    except Exception as e:
        log.warning("chroma_delete_failed", err=type(e).__name__)


def drop_collection(kb_id: str | uuid.UUID) -> None:
    with contextlib.suppress(Exception):
        get_client().delete_collection(collection_name(kb_id))


def stats(kb_id: str | uuid.UUID) -> dict[str, Any]:
    try:
        col = get_or_create_collection(kb_id)
        return {"name": col.name, "count": col.count()}
    except Exception as e:
        return {"error": type(e).__name__}


def heartbeat() -> bool:
    try:
        get_client().heartbeat()
        return True
    except Exception:
        return False
