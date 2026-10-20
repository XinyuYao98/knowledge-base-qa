"""上传路由。

    POST /api/v1/kbs/{kb_id}/documents     multipart 单次上传

## P5 决策：只做直传，不做分片（Q4 选 A）

理由：演示用的文档在几 MB 级别，分片是给 500MB 年报表准备的场景，
现在做没有真实文件能压测。

**上限由 `MAX_SINGLE_FILE_BYTES` 控制（默认 200MB）。**
超过时返回明确的 413，而不是等传输完才失败。

## 文件落盘的三步

1. 读入内存 → 算 sha256（秒传判断）
2. 校验配额（用户已用 + 本次 <= 配额）
3. 写到 `data/files/{kb_id}/{sha前16位}{ext}`

**为什么先算 hash 再落盘**：如果内容已存在，就不必写第二份。
P4 的 `index_cli` 也是这个顺序。
"""

from __future__ import annotations

import hashlib
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Request, UploadFile
from sqlalchemy import text

from app.api.deps import DBSession, client_ip
from app.core.config import settings
from app.core.errors import AppError, ErrorClass
from app.core.logging import get_logger
from app.core.ratelimit import check_upload
from app.db.repos import DocumentRepo, JobRepo
from app.rag.parsers import SUPPORTED_EXTENSIONS

log = get_logger("api.upload")

router = APIRouter(prefix="/kbs/{kb_id}/documents", tags=["documents"])

#: 分块读取大小：1MB。一次性 read() 会把 200MB 全塞进内存。
_READ_CHUNK = 1 << 20


@router.post("", status_code=202)
async def upload_document(
    kb_id: uuid.UUID,
    request: Request,
    session: DBSession,
    file: UploadFile = File(...),
) -> dict:
    """上传文件并入队。返回 202（已接受，索引异步进行）。"""
    ip = client_ip(request)
    limit = await check_upload(ip)
    if not limit.allowed:
        raise AppError(
            "rate_limited",
            limit.reason,
            http_status=429,
            error_class=ErrorClass.RETRYABLE,
            detail={"retry_after": limit.retry_after},
        )

    # ---- 1. 校验扩展名 ----
    filename = file.filename or "unnamed"
    ext = Path(filename).suffix.lower().lstrip(".")
    if ext not in SUPPORTED_EXTENSIONS:
        raise AppError(
            "unsupported_format",
            f"不支持 .{ext} 格式。支持：{', '.join('.' + e for e in SUPPORTED_EXTENSIONS)}",
            http_status=415,
        )

    # ---- 2. 确认知识库属于当前用户（RLS 已保证，但缺省时给明确 404）----
    kb = await session.execute(
        text("SELECT name, quota_bytes, used_bytes FROM knowledge_bases WHERE id = :id"),
        {"id": kb_id},
    )
    kb_row = kb.mappings().first()
    if kb_row is None:
        raise AppError("kb_not_found", "知识库不存在", http_status=404)

    # ---- 3. 流式读取 + 算 hash + 卡上限 ----
    tmp_path = settings.uploads_dir / f"tmp_{uuid.uuid4().hex}{Path(filename).suffix}"
    tmp_path.parent.mkdir(parents=True, exist_ok=True)

    hasher = hashlib.sha256()
    size = 0
    try:
        with tmp_path.open("wb") as out:
            while chunk := await file.read(_READ_CHUNK):
                size += len(chunk)
                if size > settings.MAX_SINGLE_FILE_BYTES:
                    raise AppError(
                        "file_too_large",
                        f"文件超过上限 {settings.MAX_SINGLE_FILE_BYTES // (1024 * 1024)} MB",
                        http_status=413,
                    )
                hasher.update(chunk)
                out.write(chunk)
    except AppError:
        tmp_path.unlink(missing_ok=True)
        raise

    if size == 0:
        tmp_path.unlink(missing_ok=True)
        raise AppError("empty_file", "文件为空", http_status=400)

    sha = hasher.hexdigest()

    # ---- 4. 配额检查 ----
    used = int(kb_row["used_bytes"])
    quota = int(kb_row["quota_bytes"])
    if used + size > quota:
        tmp_path.unlink(missing_ok=True)
        raise AppError(
            "quota_exceeded",
            f"配额不足：已用 {used // (1024 * 1024)} MB，上限 {quota // (1024 * 1024)} MB",
            http_status=413,
            detail={"used_bytes": used, "quota_bytes": quota},
        )

    # ---- 5. 秒传 ----
    docs = DocumentRepo(session)
    existing = await docs.find_by_sha(kb_id, sha)
    if existing is not None:
        tmp_path.unlink(missing_ok=True)
        log.info("upload_dedup", doc_id=str(existing["id"]), sha=sha[:12])

        # 已存在但未 ready（上次失败/中断）→ 重新入队
        if existing["status"] not in ("ready",):
            await JobRepo(session).enqueue(
                document_id=existing["id"],
                user_id=existing["user_id"],
            )
            return {
                "document_id": str(existing["id"]),
                "status": "requeued",
                "message": "内容已存在，已重新入队索引",
            }

        return {
            "document_id": str(existing["id"]),
            "status": "exists",
            "message": "内容已存在，直接复用",
        }

    # ---- 6. 落盘 ----
    dest_dir = settings.files_dir / str(kb_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{sha[:16]}{Path(filename).suffix}"
    shutil.move(str(tmp_path), dest)
    storage_key = str(dest.relative_to(settings.files_dir)).replace("\\", "/")

    # ---- 7. 建文档 + 入队 ----
    # 走到这里说明 existing 必为 None（秒传分支已 return）
    uid = await _uid(session)
    doc_id = await docs.create(
        kb_id=kb_id,
        user_id=uid,
        filename=filename,
        mime=file.content_type or "application/octet-stream",
        ext=ext,
        size_bytes=size,
        sha256=sha,
        storage_key=storage_key,
    )
    job_id = await JobRepo(session).enqueue(document_id=doc_id, user_id=uid)

    # ---- 8. 更新配额用量 ----
    await session.execute(
        text(
            "UPDATE knowledge_bases SET used_bytes = used_bytes + :s, "
            "doc_count = doc_count + 1, updated_at = now() WHERE id = :id"
        ),
        {"s": size, "id": kb_id},
    )

    log.info(
        "upload_accepted",
        doc_id=str(doc_id),
        job_id=str(job_id),
        size=size,
        filename=filename,
    )

    return {
        "document_id": str(doc_id),
        "job_id": str(job_id),
        "status": "pending",
        "size_bytes": size,
        "filename": filename,
    }


async def _uid(session) -> uuid.UUID:  # type: ignore[no-untyped-def]
    """取当前 RLS 上下文里的用户 id。

    不用依赖注入是因为在同一个事务里已经有 session，再走一次依赖
    会开第二个连接 —— 而 RLS 上下文是「连接级」的，第二个连接拿不到。
    """
    row = await session.execute(text("SELECT current_setting('app.user_id')::uuid"))
    return row.scalar_one()
