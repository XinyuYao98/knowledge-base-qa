#!/usr/bin/env python3
"""开发服务器启动器（Windows 必需）。

**为什么不能直接用 `uvicorn app.server:app`**：

uvicorn 的 CLI 内部走 `asyncio.run()`，在 Python 3.13 上它会用 `loop_factory`
创建 ProactorEventLoop，**不读全局策略**。而 psycopg 异步不支持 Proactor。

所以这里用 uvicorn 的编程接口 `uvicorn.Server`，自己控制事件循环。

用法：
    python scripts/dev.py              # 127.0.0.1:8080，热重载
    python scripts/dev.py --port 8090
    python scripts/dev.py --no-reload
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import uvicorn  # noqa: E402

from app.core.platform import selector_loop_factory  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--reload", dest="reload", action="store_true", default=True)
    ap.add_argument("--no-reload", dest="reload", action="store_false")
    ap.add_argument("--log-level", default="info")
    args = ap.parse_args()

    config = uvicorn.Config(
        "app.main:app",
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        reload=args.reload,
        reload_dirs=[str(ROOT / "backend" / "app")] if args.reload else None,
    )
    server = uvicorn.Server(config)

    # 关键：用 selector 循环跑，而不是 server.run()（后者内部走 asyncio.run）
    loop = selector_loop_factory()
    try:
        loop.run_until_complete(server.serve())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
