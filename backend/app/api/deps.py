"""API 依赖注入。

## 为什么集中在这里

路由函数只关心「我要一个当前用户」，不该关心：
- token 从哪个 header 解析
- 数据库会话怎么开
- RLS 上下文怎么设

这些全在依赖里做掉。路由里出现 `get_session_factory` 就是设计漏了。

## 当前用户 → RLS 会话

`current_user` 依赖返回用户 id，业务路由再用 `user_session(user_id)`
开会话。**两步分开**是因为有些路由（如 kb 列表）需要先鉴权再开库，

而鉴权本身不能走 RLS 会话（还不知道 user_id）。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.security import decode_token
from app.db.session import get_session_factory, set_session_user

log = get_logger("api.deps")


async def current_user_id(
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """从 `Authorization: Bearer <token>` 解析用户 id。

    所有 401 都用同一个 code `unauthorized`，**不区分**「没带 token」
    「token 过期」「token 伪造」—— 区分会泄露信息给攻击者。
    """
    if not authorization:
        raise AppError(
            "unauthorized", "请先登录", http_status=401
        )

    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise AppError("unauthorized", "认证头格式不正确", http_status=401)

    try:
        user_id = decode_token(parts[1], expected_type="access")
    except Exception as e:
        log.info("token_rejected", err=type(e).__name__)
        raise AppError("unauthorized", "登录已失效，请重新登录", http_status=401) from e

    try:
        uuid.UUID(user_id)
    except ValueError as e:
        raise AppError("unauthorized", "登录信息无效", http_status=401) from e

    return user_id


CurrentUserId = Annotated[str, Depends(current_user_id)]


async def db_session(user_id: CurrentUserId) -> AsyncIterator[AsyncSession]:
    """带 RLS 上下文的会话。业务路由统一用它。"""
    factory = get_session_factory()
    async with factory() as session, session.begin():
        await set_session_user(session, user_id)
        yield session


DBSession = Annotated[AsyncSession, Depends(db_session)]


async def current_user(user_id: CurrentUserId, session: DBSession) -> dict:
    """当前用户的完整信息（来自 users 表）。"""
    row = await session.execute(
        text("SELECT id, username, created_at, is_active FROM users WHERE id = :id"),
        {"id": user_id},
    )
    m = row.mappings().first()
    if m is None:
        raise AppError("unauthorized", "用户不存在", http_status=401)
    if not m["is_active"]:
        raise AppError("account_disabled", "账号已停用", http_status=403)
    return dict(m)


CurrentUser = Annotated[dict, Depends(current_user)]


def client_ip(request: Request) -> str:
    """取客户端 IP。

    **只信任直连 IP，不读 X-Forwarded-For** —— 除非明确知道在反向代理后面。
    否则攻击者伪造一个 header 就能绕开所有 IP 限流。
    """
    if request.client is None:
        return "unknown"
    return request.client.host
