"""数据库连接与会话。

**两个 Windows 专有坑，都在这里处理：**

1. `psycopg` 异步模式不支持 ProactorEventLoop（Windows 默认）。
   → 通过 `app.core.platform.selector_loop_factory()` 显式指定循环类型。

2. **engine 用延迟初始化**：模块导入时不建连接，第一次使用时才建，
   并在建之前做循环兼容性自检（fail fast，好过等到第一条 SQL 才报错）。

RLS 的关键：每个请求必须在**同一连接**上先 `SET LOCAL app.user_id`，再执行查询。
我们用 `set_config(..., is_local=true)` 下发，作用域限于当前事务。
忘记设置时查询返回空 —— 这是安全默认值，不是 bug。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings
from app.core.logging import get_logger
from app.core.platform import assert_event_loop_compatible

log = get_logger("db")


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """延迟创建引擎。第一次调用时才建连接池。"""
    global _engine
    if _engine is None:
        # 建连接之前自检：循环不兼容就立刻报错，而不是等到第一个 SQL 查询
        assert_event_loop_compatible(where="app.db.session.get_engine")
        _engine = create_async_engine(
            settings.DATABASE_URL,
            pool_size=settings.DB_POOL_SIZE,
            max_overflow=settings.DB_MAX_OVERFLOW,
            pool_timeout=settings.DB_POOL_TIMEOUT,
            pool_recycle=settings.DB_POOL_RECYCLE,
            pool_pre_ping=True,
            connect_args={"connect_timeout": settings.DB_CONNECT_TIMEOUT},
            echo=False,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(), expire_on_commit=False, autoflush=False
        )
    return _session_factory


@asynccontextmanager
async def user_session(user_id: str | None) -> AsyncIterator[AsyncSession]:
    """返回已绑定 RLS 上下文的会话。

    user_id=None 时设置空串，RLS 策略里 `current_setting('app.user_id', true)`
    与 `NULLIF(..., '')::uuid` 比较结果为 NULL → 查不到任何业务行。
    """
    factory = get_session_factory()
    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.user_id', :uid, true)"),
            {"uid": user_id or ""},
        )
        yield session


@asynccontextmanager
async def system_session() -> AsyncIterator[AsyncSession]:
    """给 worker / 后台任务用：**调用方需自行 `set_config` 指定身份**。

    直接用它写业务表会被 RLS 拦住（这是对的）。
    需要代表某个用户操作时，用 `as_user()`。
    """
    factory = get_session_factory()
    async with factory() as session, session.begin():
        yield session


@asynccontextmanager
async def as_user(user_id: str | uuid.UUID) -> AsyncIterator[AsyncSession]:
    """代表某个用户开启会话（含 RLS 上下文）。

    ## worker 为什么需要这个

    worker 是后台任务，不是用户请求，但它要写 `documents` / `chunks`，
    这两张表都有 RLS 策略 `user_id = current_setting('app.user_id')`。

    不设置这个上下文，插入会报：

        InsufficientPrivilege: new row violates row-level security policy

    实测踩过（P4 集成测试）。这不是 bug，是 RLS 在正常工作 ——
    修的方向不是关掉 RLS，而是让 worker 显式声明「我在替哪个用户干活」。
    """
    factory = get_session_factory()
    uid = str(user_id)
    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.user_id', :uid, true)"),
            {"uid": uid},
        )
        yield session


async def set_session_user(session: AsyncSession, user_id: str | uuid.UUID) -> None:
    """在**已有事务**里设置 RLS 上下文。

    用于测试或多表操作时，不想为每个操作开新会话的场景。
    必须在 `session.begin()` 之后调用（`is_local=true` 依赖事务）。
    """
    await session.execute(
        text("SELECT set_config('app.user_id', :uid, true)"),
        {"uid": str(user_id)},
    )


async def db_healthcheck() -> dict[str, Any]:
    try:
        async with get_engine().connect() as conn:
            ok = (await conn.execute(text("SELECT 1"))).scalar_one()
        return {"db": "ok" if ok == 1 else "unknown"}
    except Exception as e:
        return {"db": f"error: {type(e).__name__}: {e}"}


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
