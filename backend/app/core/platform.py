"""Windows 事件循环兼容层。

## 问题

`psycopg` 的异步模式**不支持** Windows 默认的 `ProactorEventLoop`：

```
InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run in async mode
```

## 为什么"设置策略"不管用

Python 3.13 起，`asyncio.run(coro)` 用 `loop_factory` 创建循环，**不读全局策略**。
所以 `asyncio.set_event_loop_policy(WindowsSelectorEventLoopPolicy())` 之后，
`asyncio.run()` 仍然建 ProactorEventLoop —— 策略设置被静默忽略。

`uvicorn.run()` 同理，它直接调 `asyncio.run()`。

## 正确做法

不用策略，而是显式把 `loop_factory` 传给 `asyncio.run()`。
本模块提供：

- `selector_loop_factory()`：传给 `asyncio.run(..., loop_factory=...)`
- `run()`：包装 `asyncio.run`，自动带正确工厂
- `assert_event_loop_compatible()`：启动时自检，策略没生效就明确报错

## 代价（必须知道）

SelectorEventLoop 在 Windows 上**不支持子进程**（`asyncio.create_subprocess_*`）。
本项目不用子进程（PDF 解析在进程内完成，不调用 LibreOffice 等外部命令）。
若将来需要调用外部命令，用 `asyncio.to_thread(subprocess.run, ...)` 走线程池。
"""

from __future__ import annotations

import asyncio
import selectors
import sys
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


def selector_loop_factory() -> asyncio.AbstractEventLoop:
    """返回一个基于 select 的事件循环（psycopg 兼容）。

    非 Windows 平台保持默认行为。
    """
    if sys.platform != "win32":
        return asyncio.new_event_loop()
    return asyncio.SelectorEventLoop(selectors.SelectSelector())


def run(coro: Coroutine[Any, Any, T]) -> T:
    """替代 `asyncio.run()`，保证 psycopg 能用。

    注意：`loop_factory` 参数需要 Python 3.12+。本项目的 requires-python 是
    >=3.11，因此对 3.11 回退到「设置策略 + 手动跑循环」。
    """
    if sys.version_info >= (3, 12):
        return asyncio.run(coro, loop_factory=selector_loop_factory)

    # Python 3.11 回退路径
    from asyncio import windows_events

    asyncio.set_event_loop_policy(windows_events.WindowsSelectorEventLoopPolicy())
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def is_running_loop_compatible() -> bool:
    """检查当前运行中的循环是否与 psycopg 兼容。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return True  # 没有运行中的循环，无从判断
    if sys.platform != "win32":
        return True
    return not type(loop).__name__.startswith("Proactor")


def assert_event_loop_compatible(where: str = "") -> None:
    """启动自检：循环不兼容时立刻抛出明确错误，而不是等到第一个 SQL 查询。
    """
    if is_running_loop_compatible():
        return
    hint = (
        "当前事件循环是 ProactorEventLoop，psycopg 异步模式不支持。\n"
        "修复方式：不要用 `asyncio.run(...)` 或 `uvicorn.run(...)` 的默认循环，\n"
        "改用 `app.core.platform.run(...)`，或用 `scripts/dev.py` 启动服务。\n"
        f"位置：{where or '未指定'}"
    )
    raise RuntimeError(hint)
