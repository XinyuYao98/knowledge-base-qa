"""跨模块基础设施：配置、安全、日志、埋点、异常分类。

这一层不依赖任何业务模块，可被 api / services / rag / workers 任意引用。
"""

from app.core.config import settings
from app.core.errors import AppError, ErrorClass
from app.core.logging import bind_trace, configure_logging, get_logger, new_trace_id

__all__ = [
    "AppError",
    "ErrorClass",
    "bind_trace",
    "configure_logging",
    "get_logger",
    "new_trace_id",
    "settings",
]
