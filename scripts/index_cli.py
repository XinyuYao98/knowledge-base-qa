#!/usr/bin/env python3
"""P4 索引链路 CLI：建库 → 上传 → 入队 → worker 消费 → 查状态。

## 为什么要有这个 CLI

P4 的验收标准是「上传一个文件跑到 ready」。API 要到 P5 才有，
先用 CLI 把链路跑通，能最早暴露「落库字段对不上」「Chroma 写不进去」这类问题。

## 用法

    python scripts/index_cli.py setup                      # 建测试用户+知识库
    python scripts/index_cli.py upload <文件> [--kb ID]     # 上传并入队
    python scripts/index_cli.py worker --once              # 跑一轮 worker
    python scripts/index_cli.py worker                     # 常驻
    python scripts/index_cli.py status                     # 文档/任务状态
    python scripts/index_cli.py show <doc_id>              # 单文档详情
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import hashlib
import shutil
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import text  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.platform import assert_event_loop_compatible, selector_loop_factory  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.repos import DocumentRepo, JobRepo  # noqa: E402
from app.db.session import get_session_factory, set_session_user  # noqa: E402
from app.index import chroma_store  # noqa: E402
from app.rag.parsers.dispatcher import SUPPORTED_EXTENSIONS  # noqa: E402
from app.workers.ingest_worker import Worker  # noqa: E402

TEST_USER = "p4tester"
TEST_PASSWORD = "test1234"
TEST_KB_NAME = "P4 测试库"


def _ensure_user_kb_sync() -> tuple[str, str]:
    """建（或复用）测试用户与知识库，返回 (user_id, kb_id)。

    **必须用管理员连接**：users 表在 RLS 保护下，且策略只允许
    `id = current_setting('app.user_id')` —— 应用角色连自己的用户都插不进去。

    这不矛盾：注册用户属于「系统操作」，真实 API 会通过
    SECURITY DEFINER 函数或管理员连接完成，不走应用角色的普通会话。
    """
    with _admin_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE username = %s", (TEST_USER,))
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "INSERT INTO users (username, password_hash) VALUES (%s, %s) RETURNING id",
                (TEST_USER, hash_password(TEST_PASSWORD)),
            )
            user_id = cur.fetchone()[0]
            print(f"  建用户 {TEST_USER}（密码 {TEST_PASSWORD}）")
        else:
            user_id = row[0]

        cur.execute(
            "SELECT id FROM knowledge_bases WHERE user_id = %s AND name = %s",
            (user_id, TEST_KB_NAME),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "INSERT INTO knowledge_bases (user_id, name, quota_bytes) "
                "VALUES (%s, %s, %s) RETURNING id",
                (user_id, TEST_KB_NAME, settings.KB_QUOTA_BYTES),
            )
            kb_id = cur.fetchone()[0]
            print(f"  建知识库「{TEST_KB_NAME}」")
        else:
            kb_id = row[0]

    return str(user_id), str(kb_id)


@asynccontextmanager
async def user_tx(user_id: str):
    """带 RLS 上下文的会话 —— 所有业务表读写都要走这里。"""
    factory = get_session_factory()
    async with factory() as s, s.begin():
        await set_session_user(s, user_id)
        yield s


def _admin_conn():
    import psycopg

    return psycopg.connect(settings.DATABASE_ADMIN_URL, autocommit=True, connect_timeout=5)


async def cmd_setup(_args: argparse.Namespace) -> int:
    print("[setup]")
    uid, kid = _ensure_user_kb_sync()
    print(f"  user_id = {uid}")
    print(f"  kb_id   = {kid}")
    print(f"\n下一步：python scripts/index_cli.py upload <文件> --kb {kid}")
    return 0


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def cmd_upload(args: argparse.Namespace) -> int:
    src = Path(args.file)
    if not src.exists():
        print(f"文件不存在：{src}", file=sys.stderr)
        return 2

    ext = src.suffix.lower().lstrip(".")
    if ext not in SUPPORTED_EXTENSIONS:
        print(f"不支持的格式 .{ext}", file=sys.stderr)
        return 2

    uid, kid = _ensure_user_kb_sync()
    if args.kb:
        kid = args.kb

    sha = _sha256(src)
    size = src.stat().st_size

    async with user_tx(uid) as s:
        docs = DocumentRepo(s)
        jobs = JobRepo(s)

        # 秒传：同库同内容直接复用
        existing = await docs.find_by_sha(kid, sha)
        if existing:
            print(f"  内容已存在（秒传）：doc_id={existing['id']} status={existing['status']}")
            if existing["status"] != "ready":
                await jobs.enqueue(document_id=existing["id"], user_id=uid)
                print("  已重新入队")
            return 0

        # 复制到存储目录
        dest_dir = settings.files_dir / kid
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{sha[:16]}{src.suffix}"
        shutil.copy2(src, dest)
        storage_key = str(dest.relative_to(settings.files_dir)).replace("\\", "/")

        doc_id = await docs.create(
            kb_id=kid,
            user_id=uid,
            filename=src.name,
            mime="application/octet-stream",
            ext=ext,
            size_bytes=size,
            sha256=sha,
            storage_key=storage_key,
        )
        job_id = await jobs.enqueue(document_id=doc_id, user_id=uid)

    print(f"  doc_id  = {doc_id}")
    print(f"  job_id  = {job_id}")
    print(f"  storage = {storage_key}")
    print("\n下一步：python scripts/index_cli.py worker --once")
    return 0


async def cmd_worker(args: argparse.Namespace) -> int:
    w = Worker(once=args.once, concurrency=args.concurrency)
    if args.once:
        print("[worker] 单次模式，处理完队列即退出\n")
    else:
        print("[worker] 常驻模式，Ctrl+C 停止\n")
    await w.run()
    return 0


async def cmd_status(_args: argparse.Namespace) -> int:
    async with user_tx(uid) as s:
            rows = await s.execute(
                text(
                    """
                    SELECT d.id, d.filename, d.status, d.chunk_count,
                           d.error_code, k.name AS kb_name
                      FROM documents d
                      JOIN knowledge_bases k ON k.id = d.kb_id
                     ORDER BY d.created_at DESC
                     LIMIT 20
                    """
                )
            )
            docs = rows.mappings().all()

            js = await JobRepo(s).stats()

    print("=" * 78)
    print("文档状态")
    print("=" * 78)
    if not docs:
        print("（无文档）")
    else:
        print(f"{'状态':<14} {'块':>5} {'文件':<34} {'错误'}")
        print("-" * 78)
        for d in docs:
            err = d["error_code"] or ""
            print(
                f"{d['status']:<14} {d['chunk_count']:>5} "
                f"{(d['filename'] or '')[:34]:<34} {err}"
            )

    print("\n任务队列：")
    if not js:
        print("  （空）")
    else:
        for st, n in sorted(js.items()):
            print(f"  {st:<14} {n}")

    print("\nChroma：", "ok" if chroma_store.heartbeat() else "不可用")
    return 0


async def cmd_show(args: argparse.Namespace) -> int:
    factory = get_session_factory()
    async with factory() as s:
        async with s.begin():
            doc = await DocumentRepo(s).get(args.doc_id)
            if doc is None:
                print(f"文档不存在：{args.doc_id}", file=sys.stderr)
                return 2

            rows = await s.execute(
                text(
                    "SELECT seq, chunk_type, page_from, heading_path, "
                    "       left(text, 60) AS preview "
                    "  FROM chunks WHERE document_id = :id ORDER BY seq LIMIT 10"
                ),
                {"id": doc["id"]},
            )
            chunks = rows.mappings().all()
            n = await s.execute(
                text("SELECT count(*) FROM chunks WHERE document_id = :id"),
                {"id": doc["id"]},
            )
            total = n.scalar_one()

    print(f"文档：{doc['filename']}")
    print(f"  状态      {doc['status']}")
    print(f"  块数      {doc['chunk_count']}（表中实际 {total}）")
    print(f"  向量模型  {doc['embed_model'] or '—'}")
    print(f"  错误      {doc['error_code'] or '—'} {doc['error_detail'] or ''}")
    meta = doc.get("parse_meta") or {}
    health = meta.get("health") or {}
    if health:
        print(f"  健康分    {health.get('score')}（{health.get('level')}）")
        for issue in health.get("issues", [])[:3]:
            print(f"    · {issue}")
    stages = meta.get("stages") or {}
    if stages:
        print("  阶段耗时：")
        for k in ("parse", "chunk", "embed"):
            if k in stages:
                print(f"    {k:<6} {stages[k].get('duration_ms', 0):>8.0f}ms")

    print("\n前 10 个块：")
    for c in chunks:
        print(f"  #{c['seq']:<4} {c['chunk_type']:<10} p{c['page_from'] or '-':<4} "
              f"{(c['preview'] or '').replace(chr(10), ' ')[:56]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="P4 索引链路 CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_setup = sub.add_parser("setup", help="建测试用户与知识库")
    p_setup.set_defaults(func=cmd_setup)

    p_up = sub.add_parser("upload", help="上传文件并入队")
    p_up.add_argument("file")
    p_up.add_argument("--kb", default=None)
    p_up.set_defaults(func=cmd_upload)

    p_w = sub.add_parser("worker", help="跑 worker")
    p_w.add_argument("--once", action="store_true")
    p_w.add_argument("--concurrency", type=int, default=None)
    p_w.set_defaults(func=cmd_worker)

    p_st = sub.add_parser("status", help="查看状态")
    p_st.set_defaults(func=cmd_status)

    p_sh = sub.add_parser("show", help="单文档详情")
    p_sh.add_argument("doc_id")
    p_sh.set_defaults(func=cmd_show)

    args = ap.parse_args()

    assert_event_loop_compatible(where="index_cli")
    loop = selector_loop_factory()
    try:
        return loop.run_until_complete(args.func(args))
    finally:
        loop.close()


if __name__ == "__main__":
    raise SystemExit(main())
