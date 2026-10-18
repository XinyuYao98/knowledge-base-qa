"""异常分类与统一错误结构。

设计依据是架构文档 §9 的错误处理矩阵。核心思想：
**分类错误是最常见的翻车点** —— 把 401 当可重试会打爆鉴权，
把 429 当终止会白白浪费一次可能成功的重试。

三类，处理方式完全不同：

| 类别 | 含义 | 处理 | 是否重试 |
|---|---|---|---|
| RETRYABLE | 临时性、幂等的失败 | 指数退避 + 抖动 | 是 |
| NEEDS_HUMAN | 系统无法自行决策 | 暂停 + 转人工 | 否 |
| FATAL | 继续下去没有意义 | 立即终止 + 明确原因 | 否 |
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorClass(StrEnum):
    RETRYABLE = "retryable"
    NEEDS_HUMAN = "needs_human"
    FATAL = "fatal"


class AppError(Exception):
    """业务异常基类。

    code 用于前端分支处理，message 用于直接展示给用户，
    error_class 决定上层是重试、转人工还是终止。
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        error_class: ErrorClass = ErrorClass.FATAL,
        http_status: int = 400,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.error_class = error_class
        self.http_status = http_status
        self.detail = detail or {}

    @property
    def retryable(self) -> bool:
        return self.error_class == ErrorClass.RETRYABLE

    def to_payload(self, trace_id: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "trace_id": trace_id,
        }
        if self.detail:
            payload["detail"] = self.detail
        return payload


# ---------------------------------------------------------------------------
# 具体异常（按架构文档 §9 的矩阵组织）
# ---------------------------------------------------------------------------

class QuotaError(AppError):
    def __init__(self, code: str, message: str, http_status: int = 409) -> None:
        super().__init__(code, message, error_class=ErrorClass.FATAL, http_status=http_status)


class UploadError(AppError):
    def __init__(self, code: str, message: str, http_status: int = 400) -> None:
        super().__init__(code, message, error_class=ErrorClass.FATAL, http_status=http_status)


class ParseError(AppError):
    """解析失败。retryable 由具体场景决定（例如 IO 抖动可重试，格式不支持不可重试）。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        error_class: ErrorClass = ErrorClass.FATAL,
    ) -> None:
        super().__init__(code, message, error_class=error_class, http_status=422)


class EmbeddingError(AppError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        error_class: ErrorClass = ErrorClass.RETRYABLE,
    ) -> None:
        super().__init__(code, message, error_class=error_class, http_status=503)


class LlmError(AppError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        error_class: ErrorClass = ErrorClass.FATAL,
        http_status: int = 502,
    ) -> None:
        super().__init__(code, message, error_class=error_class, http_status=http_status)


class AuthError(AppError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, error_class=ErrorClass.FATAL, http_status=401)


class NotFoundError(AppError):
    def __init__(self, resource: str, ident: str = "") -> None:
        super().__init__(
            f"{resource}_not_found",
            f"{resource} 不存在" + (f"（{ident}）" if ident else ""),
            error_class=ErrorClass.FATAL,
            http_status=404,
        )


class RateLimitedError(AppError):
    def __init__(self, message: str, retry_after: int = 60) -> None:
        super().__init__(
            "rate_limited",
            message,
            error_class=ErrorClass.RETRYABLE,
            http_status=429,
            detail={"retry_after": retry_after},
        )
