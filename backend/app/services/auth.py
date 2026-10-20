"""认证服务：注册、登录。

## 注册为什么不能直接 INSERT

`users` 表受 RLS 保护，策略 `id = current_setting('app.user_id')`。
应用角色插入时 `app.user_id` 为空 → 策略判定 false → 直接报
`InsufficientPrivilege`。P4 阶段实测确认过。

所以注册走 `auth_register()` 这个 SECURITY DEFINER 函数（见 003_auth.sql）。

**为什么不用管理员连接**：那等于让 Web 进程持有 superuser，
一次 SQL 注入就全库沦陷。SECURITY DEFINER 把提权限制在
「只能往 users 插一行」的范围内。

## 登录时序上的两个坑

### 坑 1：用户不存在时也要跑一次密码校验

如果用户不存在就立刻返回 401，响应时间明显短于「用户存在但密码错」，
攻击者可以用**响应时间差枚举用户名**。

所以用户不存在时，仍对一个固定的假 hash 跑一次 verify，
让两条路径耗时接近。

### 坑 2：错误信息不能区分「用户不存在」和「密码错误」

同上，统一返回「用户名或密码错误」。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.security import (
    create_access_token,
    hash_password,
    validate_password_policy,
    verify_password,
)
from app.db.session import get_session_factory

log = get_logger("auth")

#: 用户不存在时用来比对密码的假 hash。
#: 目的见模块 docstring「坑 1」：让两条失败路径耗时接近。
#:
#: 这是一个 argon2id 格式的字符串，对应明文是随机的 —— 永远匹配不上。
_DUMMY_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=2$"
    "c29tZS1yYW5kb20tc2FsdA$"
    "7Yb0dQ0YQ0FqZ0Z0Z0Z0Z0Z0Z0Z0Z0Z0Z0Z0Z0Z0Z0"
)


@dataclass(slots=True)
class AuthResult:
    user_id: str
    username: str
    access_token: str
    expires_in: int


async def register(username: str, password: str) -> AuthResult:
    """注册新用户并直接返回 token（注册即登录）。"""
    username = username.strip()

    if not (3 <= len(username) <= 32):
        raise AppError("invalid_username", "用户名长度需在 3-32 个字符之间")

    if not username.replace("_", "").replace("-", "").isalnum():
        raise AppError("invalid_username", "用户名只能包含字母、数字、下划线和短横线")

    try:
        validate_password_policy(password)
    except ValueError as e:
        # 策略函数抛 ValueError，必须转成 AppError，
        # 否则会冒到全局异常处理器变成 500（实测踩到）
        raise AppError("weak_password", str(e), http_status=400) from e

    factory = get_session_factory()
    async with factory() as s, s.begin():
        row = await s.execute(
            text("SELECT auth_register(:u, :p) AS uid"),
            {"u": username, "p": hash_password(password)},
        )
        uid = row.scalar_one()

        if uid is None:
            # 函数内捕获了唯一冲突，返回 NULL
            raise AppError("username_taken", "该用户名已被占用", http_status=409)

    log.info("user_registered", user_id=str(uid), username=username)
    return _issue(str(uid), username)


async def login(username: str, password: str) -> AuthResult:
    """登录。失败统一返回「用户名或密码错误」。"""
    username = username.strip()

    factory = get_session_factory()
    async with factory() as s, s.begin():
        row = await s.execute(
            text("SELECT * FROM auth_lookup_user(:u)"), {"u": username}
        )
        m = row.mappings().first()

    if m is None:
        # 坑 1：仍跑一次校验，抹平时间差
        verify_password(password, _DUMMY_HASH)
        log.info("login_user_not_found", username=username)
        raise AppError("invalid_credentials", "用户名或密码错误", http_status=401)

    if not verify_password(password, m["password_hash"]):
        log.info("login_bad_password", username=username)
        raise AppError("invalid_credentials", "用户名或密码错误", http_status=401)

    if not m["is_active"]:
        raise AppError("account_disabled", "账号已停用", http_status=403)

    log.info("login_ok", user_id=str(m["id"]), username=username)
    return _issue(str(m["id"]), m["username"])


def _issue(user_id: str, username: str) -> AuthResult:
    token = create_access_token(user_id, ttl_hours=settings.JWT_ACCESS_TTL_HOURS)
    return AuthResult(
        user_id=user_id,
        username=username,
        access_token=token,
        expires_in=settings.JWT_ACCESS_TTL_HOURS * 3600,
    )


async def record_attempt(
    session: AsyncSession, *, username: str, ip: str, success: bool
) -> None:
    """写登录审计。

    **失败不能影响登录本身** —— 审计是旁路，不参与主流程。
    """
    try:
        await session.execute(
            text(
                "INSERT INTO login_attempts (username, ip, success) "
                "VALUES (:u, :i, :s)"
            ),
            {"u": username[:64], "i": ip[:64], "s": success},
        )
    except Exception as e:
        log.warning("login_audit_failed", err=type(e).__name__)


def _now() -> datetime:
    return datetime.now(UTC)


__all__ = ["AuthResult", "login", "record_attempt", "register", "uuid"]
