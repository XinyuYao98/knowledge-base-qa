"""知识库路由。

    GET    /api/v1/kbs
    POST   /api/v1/kbs
    GET    /api/v1/kbs/{kb_id}
    DELETE /api/v1/kbs/{kb_id}      ← 需输入库名确认

## 删除为什么要求输入库名（Q4 决策：后端校验）

前端弹个框让用户输库名，只是 **UX**：防手滑点错。

**后端必须再校验一次** —— 否则任何能构造请求的人（包括前端 bug）
都能直接调 DELETE 删库。前端校验挡不住脚本。

校验规则：`confirm_name` 必须与库名完全一致（区分大小写）。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.api.deps import DBSession
from app.core.errors import AppError
from app.core.logging import get_logger

log = get_logger("api.kb")

router = APIRouter(prefix="/kbs", tags=["knowledge-bases"])


class KbCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)


class KbDelete(BaseModel):
    confirm_name: str = Field(min_length=1, max_length=64)


class KbOut(BaseModel):
    id: str
    name: str
    description: str
    doc_count: int
    chunk_count: int
    used_bytes: int
    quota_bytes: int
    created_at: str


def _row_to_out(m) -> KbOut:  # type: ignore[no-untyped-def]
    return KbOut(
        id=str(m["id"]),
        name=m["name"],
        description=m["description"] or "",
        doc_count=m["doc_count"],
        chunk_count=m["chunk_count"],
        used_bytes=int(m["used_bytes"]),
        quota_bytes=int(m["quota_bytes"]),
        created_at=m["created_at"].isoformat(),
    )


@router.get("", response_model=list[KbOut])
async def list_kbs(session: DBSession) -> list[KbOut]:
    rows = await session.execute(
        text(
            "SELECT id, name, description, doc_count, chunk_count, "
            "       used_bytes, quota_bytes, created_at "
            "  FROM knowledge_bases ORDER BY created_at DESC"
        )
    )
    return [_row_to_out(m) for m in rows.mappings().all()]


@router.post("", response_model=KbOut, status_code=201)
async def create_kb(body: KbCreate, session: DBSession) -> KbOut:
    name = body.name.strip()
    if not name:
        raise AppError("invalid_name", "库名不能为空")

    # 重名检查：UNIQUE(user_id, name) 也会拦，但提前判断能给出更友好的错误
    exists = await session.execute(
        text("SELECT 1 FROM knowledge_bases WHERE name = :n"), {"n": name}
    )
    if exists.first() is not None:
        raise AppError("name_taken", f"已存在同名知识库「{name}」", http_status=409)

    row = await session.execute(
        text(
            "INSERT INTO knowledge_bases (user_id, name, description, quota_bytes) "
            "VALUES (current_setting('app.user_id')::uuid, :n, :d, :q) "
            "RETURNING id, name, description, doc_count, chunk_count, "
            "          used_bytes, quota_bytes, created_at"
        ),
        {"n": name, "d": body.description.strip(), "q": 1 * 1024 * 1024 * 1024},
    )
    m = row.mappings().one()
    log.info("kb_created", kb_id=str(m["id"]), name=name)
    return _row_to_out(m)


@router.get("/{kb_id}", response_model=KbOut)
async def get_kb(kb_id: uuid.UUID, session: DBSession) -> KbOut:
    row = await session.execute(
        text(
            "SELECT id, name, description, doc_count, chunk_count, "
            "       used_bytes, quota_bytes, created_at "
            "  FROM knowledge_bases WHERE id = :id"
        ),
        {"id": kb_id},
    )
    m = row.mappings().first()
    if m is None:
        # 404 而不是 403：不暴露「存在但不属于你」这一信息
        raise AppError("kb_not_found", "知识库不存在", http_status=404)
    return _row_to_out(m)


@router.delete("/{kb_id}", status_code=204)
async def delete_kb(
    kb_id: uuid.UUID,
    body: KbDelete,
    session: DBSession,
) -> None:
    """删除知识库。必须输入库名确认（后端校验，不是只靠前端）。"""
    row = await session.execute(
        text("SELECT name FROM knowledge_bases WHERE id = :id"), {"id": kb_id}
    )
    m = row.mappings().first()
    if m is None:
        raise AppError("kb_not_found", "知识库不存在", http_status=404)

    actual = m["name"]
    if body.confirm_name != actual:
        raise AppError(
            "confirm_mismatch",
            f"库名不匹配，请输入「{actual}」以确认删除",
            http_status=400,
            detail={"expected": actual},
        )

    # 级联：documents / chunks / messages 由外键 ON DELETE CASCADE 处理
    await session.execute(text("DELETE FROM knowledge_bases WHERE id = :id"), {"id": kb_id})
    log.info("kb_deleted", kb_id=str(kb_id), name=actual)


@router.get("/{kb_id}/quality")
async def kb_quality(kb_id: uuid.UUID, session: DBSession) -> dict:
    """知识库质量概览（P8 看板的数据源）。

    数据来自 documents.parse_meta 里的 health 字段，
    不重新计算 —— 解析阶段已经算过并落库了。
    """
    row = await session.execute(
        text("SELECT id FROM knowledge_bases WHERE id = :id"), {"id": kb_id}
    )
    if row.first() is None:
        raise AppError("kb_not_found", "知识库不存在", http_status=404)

    docs = await session.execute(
        text(
            "SELECT id, filename, status, chunk_count, parse_meta "
            "  FROM documents WHERE kb_id = :kb ORDER BY created_at DESC"
        ),
        {"kb": kb_id},
    )

    items = []
    scores: list[float] = []
    issues: dict[str, int] = {}

    for m in docs.mappings().all():
        meta = m["parse_meta"] or {}
        health = meta.get("health") or {}
        score = float(health.get("score", 0) or 0)
        if m["status"] == "ready":
            scores.append(score)
        for issue in health.get("issues", []):
            issues[issue] = issues.get(issue, 0) + 1
        items.append(
            {
                "document_id": str(m["id"]),
                "filename": m["filename"],
                "status": m["status"],
                "chunk_count": m["chunk_count"],
                "health_score": score,
                "health_level": health.get("level", "unknown"),
                "issues": health.get("issues", []),
            }
        )

    return {
        "kb_id": str(kb_id),
        "summary": {
            "doc_count": len(items),
            "ready_count": len(scores),
            "avg_health": round(sum(scores) / len(scores), 3) if scores else 0.0,
            "total_chunks": sum(i["chunk_count"] for i in items),
        },
        "documents": items,
        "top_issues": [{"issue": k, "count": v} for k, v in sorted(issues.items(), key=lambda x: -x[1])[:5]],
    }


@router.get("/{kb_id}/documents")
async def list_documents(
    kb_id: uuid.UUID,
    session: DBSession,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """文档列表（前端轮询索引进度用）。"""
    total = await session.execute(
        text("SELECT count(*) FROM documents WHERE kb_id = :kb"), {"kb": kb_id}
    )
    rows = await session.execute(
        text(
            "SELECT id, filename, ext, size_bytes, status, chunk_count, "
            "       error_code, error_detail, created_at "
            "  FROM documents WHERE kb_id = :kb "
            " ORDER BY created_at DESC LIMIT :lim OFFSET :off"
        ),
        {"kb": kb_id, "lim": limit, "off": offset},
    )

    return {
        "total": int(total.scalar_one()),
        "items": [
            {
                "id": str(m["id"]),
                "filename": m["filename"],
                "ext": m["ext"],
                "size_bytes": int(m["size_bytes"]),
                "status": m["status"],
                "chunk_count": m["chunk_count"],
                "error_code": m["error_code"],
                "error_detail": m["error_detail"],
                "created_at": m["created_at"].isoformat(),
            }
            for m in rows.mappings().all()
        ],
    }
