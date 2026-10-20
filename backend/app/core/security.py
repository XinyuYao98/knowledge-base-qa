"""密码哈希、JWT、模型密钥对称加密。

安全约束（C6）：
- 密码用 argon2id，绝不明文/可逆
- JWT 只放 user_id 与过期时间，不放角色/PII
- 用户填的 LLM api_key 用 Fernet 加密后入库，密钥来自环境变量
"""

from __future__ import annotations

import base64
import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)

ACCESS = "access"
REFRESH = "refresh"

# 密码规则（D12）：6-12 位，仅数字与英文字母，必须至少各含一个
_PASSWORD_RE = re.compile(r"^(?=.*[A-Za-z])(?=.*\d)[A-Za-z\d]{6,12}$")


class TokenError(Exception):
    """JWT 校验失败的统一异常，由 API 层转 401。"""


# ---------------------------------------------------------------------------
# 密码
# ---------------------------------------------------------------------------
def validate_password_policy(raw: str) -> None:
    """校验密码是否符合 D12 规则。不符合时抛 ValueError，message 可直接展示。"""
    if not (6 <= len(raw) <= 12):
        raise ValueError("密码长度需为 6-12 位")
    if not _PASSWORD_RE.match(raw):
        raise ValueError("密码只能包含数字和英文字母，且两者都至少出现一次")


def hash_password(raw: str) -> str:
    validate_password_policy(raw)
    return _hasher.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, raw)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------
def _encode(user_id: str, token_type: str, ttl: int) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": user_id,
        "typ": token_type,
        "iat": now,
        "exp": now + ttl,
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALG)


def create_access_token(user_id: str, *, ttl_hours: int | None = None) -> str:
    """签发 access token。

    `ttl_hours` 为 P5 新增的可选参数：配置项 `JWT_ACCESS_TTL_HOURS`
    允许不重启改有效期。不传时沿用原有 `ACCESS_TOKEN_TTL_SECONDS`。
    """
    ttl = ttl_hours * 3600 if ttl_hours else settings.ACCESS_TOKEN_TTL_SECONDS
    return _encode(user_id, ACCESS, ttl)


def create_refresh_token(user_id: str) -> str:
    return _encode(user_id, REFRESH, settings.REFRESH_TOKEN_TTL_SECONDS)


def decode_token(token: str, expected_type: str) -> str:
    """返回 user_id。类型不符、过期、签名错误一律抛 TokenError。"""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALG])
    except jwt.ExpiredSignatureError as e:
        raise TokenError("token_expired") from e
    except jwt.PyJWTError as e:
        raise TokenError("token_invalid") from e

    if payload.get("typ") != expected_type:
        raise TokenError("token_type_mismatch")
    sub = payload.get("sub")
    if not isinstance(sub, str) or not sub:
        raise TokenError("token_invalid")
    return sub


# ---------------------------------------------------------------------------
# 模型密钥加密
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SecretBox:
    """对用户自己填写的 api_key 做静态加密。"""

    _fernet: Fernet

    @classmethod
    def from_settings(cls) -> SecretBox:
        if settings.FERNET_KEY:
            return cls(Fernet(settings.FERNET_KEY.encode()))
        # 开发环境允许缺省：用 JWT_SECRET 派生一个固定密钥。
        # 这不是好做法，但比"明文落库"好；启动日志会告警。
        derived = hashlib.sha256(settings.JWT_SECRET.encode()).digest()
        return cls(Fernet(base64.urlsafe_b64encode(derived)))

    def encrypt(self, plain: str) -> str:
        return self._fernet.encrypt(plain.encode()).decode()

    def decrypt(self, cipher: str) -> str:
        try:
            return self._fernet.decrypt(cipher.encode()).decode()
        except InvalidToken as e:
            raise ValueError("secret_decrypt_failed") from e


def mask_secret(value: str) -> str:
    """只回显末 4 位。用于 API 返回，绝不返回明文。"""
    if len(value) <= 4:
        return "****"
    return "****" + value[-4:]
