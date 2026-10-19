"""P4 异步数据库测试的配置。

## 为什么这个目录单独有 conftest

这些测试需要**异步**数据库会话，而 psycopg 异步不支持 Windows 的
`ProactorEventLoop`。P1 阶段试过 `set_event_loop_policy` ——
**在 Python 3.13 上完全无效**，因为 `asyncio.run()` 用 `loop_factory`
建循环，不读全局策略。

正确做法：覆盖 pytest-asyncio 内部的 `_asyncio_loop_factory` fixture。
fixture 覆盖遵循「最近的作用域优先」，所以放在这个子目录即可，
不会污染其他不涉及异步的测试（解析、切块、清理都是同步的）。
"""

from __future__ import annotations

import asyncio
import selectors
import sys
from collections.abc import Iterator

import pytest


@pytest.fixture(scope="session")
def _asyncio_loop_factory() -> Iterator[object]:
    """创建 selector 循环，绕开 ProactorEventLoop。"""
    if sys.platform != "win32":
        yield asyncio.new_event_loop
        return

    def factory() -> asyncio.AbstractEventLoop:
        return asyncio.SelectorEventLoop(selectors.SelectSelector())

    yield factory
