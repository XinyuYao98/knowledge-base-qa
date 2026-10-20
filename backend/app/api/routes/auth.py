"""认证路由。

    POST /api/v1/auth/register
    POST /api/v1/auth/login
    GET  /api/v1/auth/me

## 为什么注册也要限流

不限流的话，攻击者可以用注册接口批量建号（撞库准备、刷配额）。
按 IP 限 5 次/小时。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from app.api.deps import CurrentUserId, client_ip
from app.core.errors import AppError, ErrorClass
from app.core.logging import get_logger
from app.core.ratelimit import check_login, check_register, reset_login
from app.db.session import as_user, get_session_factory
from app.services import auth as auth_service

log = get_logger("api.auth")

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user_id: str
    username: str


class UserResponse(BaseModel):
    id: str
    username: str
    created_at: str


@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(body: RegisterRequest, request: Request, response: Response) -> TokenResponse:
    ip = client_ip(request)

    limit = await check_register(ip)
    if not limit.allowed:
        raise AppError(
            "rate_limited",
            limit.reason,
            http_status=429,
            error_class=ErrorClass.RETRYABLE,
            detail={"retry_after": limit.retry_after},
        )

    result = await auth_service.register(body.username, body.password)

    # 注册也要记一次（便于发现批量建号）
    async with _audit_session() as s:
        await auth_service.record_attempt(
            s, username=body.username, ip=ip, success=True
        )

    return TokenResponse(
        access_token=result.access_token,
        expires_in=result.expires_in,
        user_id=result.user_id,
        username=result.username,
    )


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request, response: Response) -> TokenResponse:
    ip = client_ip(request)

    # 双维度限流：先 IP 后用户名
    limit = await check_login(ip, body.username)
    if not limit.allowed:
        # **不能**在 response 上设 header 再抛异常：
        # FastAPI 会走 exception_handler 生成新响应，之前设的 header 全丢
        # （实测踩到：429 返回了但没有 Retry-After）。
        # 正确做法是放进 detail，由全局异常处理器写 header。
        raise AppError(
            "rate_limited",
            limit.reason,
            http_status=429,
            error_class=ErrorClass.RETRYABLE,
            detail={"retry_after": limit.retry_after},
        )

    for k, v in limit.headers().items():
        response.headers[k] = v

    try:
        result = await auth_service.login(body.username, body.password)
    except AppError as e:
        # 失败也要记录（用于审计爆破行为）。
        # 用普通会话即可：login_attempts 没有 user_id 列，
        # 其策略是 `FOR INSERT WITH CHECK (true)`，不需要 RLS 上下文。
        async with _audit_session() as s:
            await auth_service.record_attempt(
                s, username=body.username, ip=ip, success=False
            )
        raise e

    # 成功：清掉该用户名的失败计数
    await reset_login(body.username, ip)

    async with _audit_session() as s:
        await auth_service.record_attempt(
            s, username=body.username, ip=ip, success=True
        )

    return TokenResponse(
        access_token=result.access_token,
        expires_in=result.expires_in,
        user_id=result.user_id,
        username=result.username,
    )


@router.get("/me", response_model=UserResponse)
async def me(user_id: CurrentUserId) -> UserResponse:
    async with as_user(user_id) as s:
        from sqlalchemy import text

        row = await s.execute(
            text("SELECT id, username, created_at FROM users WHERE id = :id"),
            {"id": user_id},
        )
        m = row.mappings().first()

    if m is None:
        raise AppError("unauthorized", "用户不存在", http_status=401)

    return UserResponse(
        id=str(m["id"]),
        username=m["username"],
        created_at=m["created_at"].isoformat(),
    )


@asynccontextmanager
async def _audit_session():
    """写审计用的普通会话。

    `login_attempts` 没有 user_id 列，策略是 `FOR INSERT WITH CHECK (true)`，
    所以不需要 RLS 上下文 —— 用普通会话即可。
    """
    factory = get_session_factory()
    async with factory() as s, s.begin():
        yield s
