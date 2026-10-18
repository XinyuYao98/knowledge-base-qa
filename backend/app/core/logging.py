"""结构化日志 + trace_id 贯穿 + PII 脱敏。

三条硬约束（架构文档 §10）：
1. 每个请求一个 trace_id，通过 contextvars 透传，日志自动带上
2. 敏感字段在 processor 层统一打码 —— 业务代码不需要记得脱敏，忘记脱敏是常态
3. 输出 JSON，便于采集到 Loki / ELK
"""

from __future__ import annotations

import logging
import re
import sys
import uuid
from contextvars import ContextVar
from typing import Any

import structlog

# ---------------------------------------------------------------------------
# 上下文变量
# ---------------------------------------------------------------------------
_trace_id: ContextVar[str] = ContextVar("trace_id", default="-")
_span_stack: ContextVar[tuple[str, ...]] = ContextVar("span_stack", default=())


def new_trace_id() -> str:
    return uuid.uuid4().hex


def bind_trace(trace_id: str | None = None) -> str:
    tid = trace_id or new_trace_id()
    _trace_id.set(tid)
    return tid


def current_trace_id() -> str:
    return _trace_id.get()


def push_span(name: str) -> None:
    _span_stack.set(_span_stack.get() + (name,))


def pop_span() -> None:
    stack = _span_stack.get()
    if stack:
        _span_stack.set(stack[:-1])


# ---------------------------------------------------------------------------
# PII 脱敏
# ---------------------------------------------------------------------------
_SENSITIVE_KEYS = (
    "password", "api_key", "apikey", "authorization", "token",
    "secret", "cookie", "credential", "fernet", "jwt",
)

_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")
_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_ID_CARD_RE = re.compile(r"(?<!\d)(\d{6})\d{8}(\d{3}[\dXx])(?!\d)")


def mask_text(value: str) -> str:
    """手机号、邮箱、身份证打码。日志与 trace 中 PII 一律不落原文（C6）。"""
    value = _PHONE_RE.sub(r"\1****\2", value)
    value = _EMAIL_RE.sub(r"\1***@\2", value)
    value = _ID_CARD_RE.sub(r"\1********\2", value)
    return value


def _mask_value(key: str, value: Any) -> Any:
    lowered = key.lower()
    if any(s in lowered for s in _SENSITIVE_KEYS):
        return "***"
    if isinstance(value, str):
        # 超长字符串截断，避免单条日志过大
        masked = mask_text(value)
        return masked if len(masked) <= 2000 else masked[:2000] + "…[truncated]"
    if isinstance(value, dict):
        return {k: _mask_value(str(k), v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask_value(key, v) for v in value]
    return value


# ---------------------------------------------------------------------------
# structlog processors
# ---------------------------------------------------------------------------
def _inject_context(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    event_dict["trace_id"] = _trace_id.get()
    stack = _span_stack.get()
    if stack:
        event_dict["span"] = stack[-1]
    return event_dict


def _sanitize(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    return {k: _mask_value(str(k), v) for k, v in event_dict.items()}


def configure_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=log_level)

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=False),
        _inject_context,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _sanitize,
    ]
    processors.append(
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if json_output
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "app") -> Any:
    return structlog.get_logger(name)
