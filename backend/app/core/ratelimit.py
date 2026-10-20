"""Redis 限流。

## 为什么自己做

项目里已经有 Redis（启动检查在用），自己实现约 30 行，
比引入 slowapi 更可控 —— 且能精确控制「按 IP + 按用户名」两个维度。

## 双维度限流（Q3 决策）

| 维度 | 阈值 | 挡住什么 |
|---|---|---|
| 按 IP | 10 次 / 1 分钟 | 单机暴力尝试 |
| 按用户名 | 5 次 / 15 分钟 | 分布式撞库（攻击者换 IP 打同一账号） |

**只做 IP 挡不住换 IP 的攻击；只做用户名挡不住扫号。两个都要。**

## 失败关闭 vs 失败开放

Redis 挂掉时选择**放行**（fail-open），原因：

- 限流是防护，不是功能。它挂了不该让用户登不进来
- 真正的高危场景（大规模爆破）在 demo 里不存在
- 但要打日志，否则出问题不知道

**如果这是生产系统，应该选 fail-closed（拒绝），因为账密爆破的代价更高。**
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from redis.asyncio import Redis

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("ratelimit")

#: 滑动窗口实现用固定窗口 + INCR，简单够用。
#: 已知缺陷：窗口边界处可能瞬时通过 2 倍请求量。
#: 对 demo 无影响；生产应换滑动窗口（ZSET）或令牌桶。


@dataclass(slots=True)
class LimitResult:
    allowed: bool
    remaining: int
    retry_after: int = 0      # 秒
    reason: str = ""

    def headers(self) -> dict[str, str]:
        h = {"X-RateLimit-Remaining": str(max(self.remaining, 0))}
        if not self.allowed:
            h["Retry-After"] = str(self.retry_after)
        return h


_redis: Redis | None = None


def _client() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(
            settings.REDIS_URL,
            socket_connect_timeout=2,
            socket_timeout=2,
            decode_responses=True,
        )
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


async def _hit(key: str, limit: int, window_seconds: int) -> LimitResult:
    """固定窗口计数。返回是否放行。"""
    now = int(time.time())
    bucket = now // window_seconds
    full_key = f"rl:{key}:{bucket}"

    try:
        client = _client()
        # pipeline 减少往返：INCR + 设置过期
        async with client.pipeline(transaction=True) as pipe:
            pipe.incr(full_key)
            pipe.expire(full_key, window_seconds + 5)
            count, _ = await pipe.execute()
    except Exception as e:
        # 失败开放：Redis 挂了不阻断登录
        log.warning("ratelimit_unavailable", err=type(e).__name__, key=key)
        return LimitResult(allowed=True, remaining=limit, reason="redis_unavailable")

    remaining = limit - int(count)
    if remaining >= 0:
        return LimitResult(allowed=True, remaining=remaining)

    retry_after = window_seconds - (now % window_seconds)
    log.info("ratelimit_hit", key=key, count=count, limit=limit)
    return LimitResult(
        allowed=False,
        remaining=0,
        retry_after=retry_after,
        reason=f"超过限制（{limit} 次 / {window_seconds} 秒）",
    )


async def check_login(ip: str, username: str) -> LimitResult:
    """登录前的双维度检查。任一维度超限即拒绝。"""
    by_ip = await _hit(f"login:ip:{ip}", settings.RATE_LIMIT_LOGIN_PER_MINUTE, 60)
    if not by_ip.allowed:
        return LimitResult(
            allowed=False,
            remaining=0,
            retry_after=by_ip.retry_after,
            reason="同一 IP 尝试过于频繁，请稍后再试",
        )

    by_user = await _hit(
        f"login:user:{username.lower()}",
        settings.RATE_LIMIT_LOGIN_USER_ATTEMPTS,
        settings.RATE_LIMIT_LOGIN_USER_WINDOW_SECONDS,
    )
    if not by_user.allowed:
        return LimitResult(
            allowed=False,
            remaining=0,
            retry_after=by_user.retry_after,
            reason="该账号尝试次数过多，请稍后再试",
        )

    return LimitResult(allowed=True, remaining=min(by_ip.remaining, by_user.remaining))


async def reset_login(username: str, ip: str) -> None:
    """登录成功后清掉该用户名的失败计数（不清 IP 计数）。

    **为什么不清 IP**：IP 计数是防「同一台机器扫多个账号」，
    一次成功登录不能证明这台机器后面不会再试别的账号。
    """
    key = f"login:user:{username.lower()}"
    try:
        client = _client()
        now = int(time.time())
        window = settings.RATE_LIMIT_LOGIN_USER_WINDOW_SECONDS
        bucket = now // window
        async with client.pipeline(transaction=True) as pipe:
            pipe.delete(f"rl:{key}:{bucket}")
            pipe.delete(f"rl:{key}:{bucket - 1}")
            await pipe.execute()
    except Exception as e:
        log.warning("ratelimit_reset_failed", err=type(e).__name__)
    del ip


async def check_register(ip: str) -> LimitResult:
    """注册限流：按 IP，1 小时 5 次。"""
    return await _hit(f"register:ip:{ip}", settings.RATE_LIMIT_REGISTER_PER_HOUR, 3600)


async def check_upload(ip: str) -> LimitResult:
    """上传限流：按 IP，1 分钟 20 次。"""
    return await _hit(f"upload:ip:{ip}", 20, 60)
