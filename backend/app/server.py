"""应用入口（给支持自定义循环的服务器用）。

Windows 上启动必须用 scripts/dev.py，因为它能控制事件循环。
直接 `uvicorn app.server:app` 在 Windows 上会因 ProactorEventLoop 失败。
"""

from __future__ import annotations

from app.main import app

__all__ = ["app"]
