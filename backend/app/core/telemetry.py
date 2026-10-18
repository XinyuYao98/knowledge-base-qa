"""轻量 span 埋点 + 计数器。

不引入 OpenTelemetry（骨架阶段没必要背这个依赖），但保留同样语义：
- span 以「组件.动作」命名，结束输出一条 json 日志
- 采集后可直接算 P95 与成功率

上线替换步骤：把 `span()` 内部换成 otel tracer.start_as_current_span，
字段名保持不变即可，日志消费方无需改。
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from app.core.logging import get_logger, pop_span, push_span

log = get_logger("span")

# 进程内单调计数。生产环境应换成 Prometheus counter。
_COUNTERS: dict[str, int] = {}


def incr(metric: str, n: int = 1) -> None:
    _COUNTERS[metric] = _COUNTERS.get(metric, 0) + n


def counter_snapshot() -> dict[str, int]:
    return dict(_COUNTERS)


def reset_counters() -> None:
    _COUNTERS.clear()


@contextmanager
def span(name: str, **fields: Any) -> Iterator[dict[str, Any]]:
    """用法：

    with span("retrieval.search", kb_id=kid) as sp:
        ...
        sp["hit_count"] = len(rows)

    异常会以 status=error 记录后**原样抛出**，不吞异常。
    """
    push_span(name)
    started = time.perf_counter()
    extra: dict[str, Any] = {}
    status = "ok"
    error_type: str | None = None
    try:
        yield extra
    except Exception as e:
        status = "error"
        error_type = type(e).__name__
        raise
    finally:
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        pop_span()
        payload: dict[str, Any] = {
            "span": name,
            "status": status,
            "latency_ms": latency_ms,
            **fields,
            **extra,
        }
        if error_type:
            payload["error_type"] = error_type
        level = "error" if status == "error" else "info"
        getattr(log, level)("span_end", **payload)
        incr(f"span.{name}.{status}")
