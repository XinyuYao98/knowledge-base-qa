"""Worker 专用的数据库连接。

## 为什么 worker 用管理员连接

`ingest_jobs` 与 `documents` 都在 RLS 保护下，策略是
`user_id = current_setting('app.user_id')`。

但 worker 的职责恰恰是**跨用户调度**：

    领任务（不知道下一个任务是哪个用户的）
    → 才知道 user_id
    → 才能设置 RLS 上下文

这是个鸡生蛋问题。三个可选方案：

| 方案 | 后果 |
|---|---|
| **A. 给应用角色 BYPASSRLS** | 应用角色整体提权，API 也能绕过隔离 —— **最差** |
| **B. 给 ingest_jobs 加「读全部」策略** | 领任务解决了，但写 documents 仍被拦，且多一条要维护的策略 |
| **C. worker 用管理员连接，API 用应用角色** | 隔离边界不变，提权仅限受信任组件 |

选 C。

## 安全边界

- 管理员连接**只在 worker 进程内使用**，不进 API、不进请求上下文
- worker 仍然是「按 user_id 显式设上下文」再写业务表 —— 不是无脑绕过
- 这是运维组件的正常形态，与「给应用提权」是两回事

## 部署要求

worker 进程需要 `DATABASE_ADMIN_URL`。生产环境应给 worker 单独的
数据库账号（`kbworker`，BYPASSRLS），而不是复用 postgres 超级用户。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.logging import get_logger
from app.core.platform import assert_event_loop_compatible

log = get_logger("worker_db")

_worker_engine: AsyncEngine | None = None
_worker_factory: async_sessionmaker[AsyncSession] | None = None


def _admin_dsn() -> str:
    """把同步 DSN 转成异步 DSN。"""
    url = settings.DATABASE_ADMIN_URL
    if url.startswith("postgresql+psycopg://"):
        return url
    return url.replace("postgresql://", "postgresql+psycopg://", 1)


def get_worker_engine() -> AsyncEngine:
    global _worker_engine
    if _worker_engine is None:
        assert_event_loop_compatible(where="worker_db.get_worker_engine")
        _worker_engine = create_async_engine(
            _admin_dsn(),
            pool_size=2,
            max_overflow=2,
            pool_pre_ping=True,
            connect_args={"connect_timeout": settings.DB_CONNECT_TIMEOUT},
            echo=False,
        )
    return _worker_engine


def get_worker_factory() -> async_sessionmaker[AsyncSession]:
    global _worker_factory
    if _worker_factory is None:
        _worker_factory = async_sessionmaker(
            bind=get_worker_engine(), expire_on_commit=False, autoflush=False
        )
    return _worker_factory


@asynccontextmanager
async def worker_session() -> AsyncIterator[AsyncSession]:
    """worker 的调度会话：可以跨用户读写。

    **写业务数据前仍要 set_config 声明身份** —— 这是为了让 RLS
    在 SQL 层面也说得通，而不是完全无视它。
    """
    factory = get_worker_factory()
    async with factory() as session, session.begin():
        yield session


async def dispose_worker_engine() -> None:
    global _worker_engine, _worker_factory
    if _worker_engine is not None:
        await _worker_engine.dispose()
        _worker_engine = None
        _worker_factory = None
