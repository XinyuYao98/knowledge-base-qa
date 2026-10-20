"""FastAPI 应用装配。

P0 只装配最小可运行骨架：健康检查、异常处理、trace 中间件、CORS。
路由按 P5-P6 逐步接入（auth / kb / upload / chat / quality / settings）。

启动检查（启动失败要早于收到流量）：
- Redis 连通性
- Chroma 连通性
- Embedding 服务连通性（缺失只告警，不阻断，见 D6-b）
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import api_router
from app.core.config import settings
from app.core.errors import AppError, ErrorClass
from app.core.logging import bind_trace, configure_logging, current_trace_id, get_logger, new_trace_id
from app.core.telemetry import counter_snapshot, span  # noqa: E402

configure_logging(settings.LOG_LEVEL, json_output=settings.APP_ENV != "dev")
log = get_logger("main")


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings.ensure_dirs()

    if settings.APP_ENV != "prod" and not settings.FERNET_KEY:
        log.warning(
            "fernet_key_missing",
            hint="开发环境将用 JWT_SECRET 派生密钥；生产环境必须设置 FERNET_KEY",
        )

    log.info(
        "startup",
        env=settings.APP_ENV,
        embed_model=settings.EMBED_MODEL,
        embed_dim=settings.EMBED_DIM,
        chroma_mode=settings.CHROMA_MODE,
    )

    # 依赖探测：失败只告警，不阻断启动（模型服务晚起是常态）
    await _probe_dependencies()

    yield

    from app.db.session import dispose_engine

    await dispose_engine()
    log.info("shutdown")


async def _probe_dependencies() -> None:
    from app.db.session import db_healthcheck

    try:
        db = await db_healthcheck()
        log.info("dependency_check", **db)
    except Exception as e:
        log.error("db_unreachable", err=type(e).__name__)

    try:
        import redis.asyncio as redis

        r = redis.from_url(settings.REDIS_URL, socket_connect_timeout=3)
        await r.ping()
        await r.aclose()
        log.info("dependency_check", redis="ok")
    except Exception as e:
        log.error("redis_unreachable", err=type(e).__name__)


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------
app = FastAPI(
    title="RAG 知识库问答系统",
    version="0.1.0",
    description=(
        "面向中文文档的 RAG 知识库问答。\n\n"
        "**注意**：问答与索引需要本地 embedding 服务在运行，"
        "详见 README 的启动顺序。"
    ),
    docs_url="/docs" if settings.APP_ENV != "prod" else None,
    redoc_url=None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Trace-Id"],
)


@app.middleware("http")
async def trace_middleware(request: Request, call_next: Any):
    trace_id = request.headers.get("X-Trace-Id") or new_trace_id()
    bind_trace(trace_id)
    started = time.perf_counter()

    response = await call_next(request)

    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    response.headers["X-Trace-Id"] = trace_id

    path = request.url.path
    if path.startswith(settings.API_PREFIX) or path.startswith("/health"):
        log.info(
            "http_request",
            method=request.method,
            path=path,
            status_code=response.status_code,
            latency_ms=latency_ms,
        )
    return response


# ---------------------------------------------------------------------------
# 异常处理
# ---------------------------------------------------------------------------
# 业务路由（P5）
app.include_router(api_router)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    log.warning(
        "app_error",
        code=exc.code,
        error_class=str(exc.error_class),
        path=request.url.path,
    )
    headers = {}
    if exc.error_class == ErrorClass.RETRYABLE:
        headers["Retry-After"] = str(exc.detail.get("retry_after", 5))
    return JSONResponse(
        status_code=exc.http_status,
        content={"detail": exc.to_payload(current_trace_id())},
        headers=headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    first = errors[0] if errors else {}
    loc = ".".join(str(x) for x in first.get("loc", []) if x not in ("body", "query", "path"))
    msg = first.get("msg", "参数不合法")
    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "code": "validation_error",
                "message": f"{loc}: {msg}" if loc else msg,
                "trace_id": current_trace_id(),
            }
        },
    )


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled_exception", path=request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "detail": {
                "code": "internal_error",
                "message": "服务异常，请稍后重试",
                "trace_id": current_trace_id(),
            }
        },
    )


# ---------------------------------------------------------------------------
# 运维端点
# ---------------------------------------------------------------------------
@app.get("/health", tags=["ops"])
async def health() -> dict[str, Any]:
    """存活检查。只看数据库与 Redis，不查外部模型服务 ——
    避免编排系统因外部模型抖动而误杀容器。
    """
    from app.db.session import db_healthcheck

    result: dict[str, Any] = {"app": "ok", "trace_id": current_trace_id()}
    with span("health.db"):
        result.update(await db_healthcheck())
    try:
        import redis.asyncio as redis

        r = redis.from_url(settings.REDIS_URL, socket_connect_timeout=3)
        await r.ping()
        await r.aclose()
        result["redis"] = "ok"
    except Exception as e:
        result["redis"] = f"error: {type(e).__name__}"
    return result


@app.get("/health/deps", tags=["ops"])
async def health_deps() -> dict[str, Any]:
    """依赖检查。查 embedding / chroma / reranker 是否可用。

    这个端点可能较慢（要真实调用模型），不用于容器健康检查，
    仅供人工排查与前端「系统状态」展示。
    """
    out: dict[str, Any] = {
        "embedding": {
            "url": settings.EMBED_BASE_URL,
            "model": settings.EMBED_MODEL,
            "dim": settings.EMBED_DIM,
        },
        "chroma": {"mode": settings.CHROMA_MODE},
        "rerank_enabled": settings.RERANK_ENABLED,
    }

    try:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as c:
            resp = await c.get(f"{settings.EMBED_BASE_URL.rstrip('/')}/models",
                               headers={"Authorization": f"Bearer {settings.EMBED_API_KEY}"})
            out["embedding"]["reachable"] = resp.status_code < 500
            out["embedding"]["status_code"] = resp.status_code
    except Exception as e:
        out["embedding"]["reachable"] = False
        out["embedding"]["error"] = type(e).__name__

    try:
        import chromadb

        if settings.CHROMA_MODE == "http":
            client = chromadb.HttpClient(host=settings.CHROMA_HOST, port=settings.CHROMA_PORT)
        else:
            client = chromadb.PersistentClient(path=str(settings.chroma_path))
        client.heartbeat()
        out["chroma"]["reachable"] = True
    except Exception as e:
        out["chroma"]["reachable"] = False
        out["chroma"]["error"] = type(e).__name__

    return out


@app.get("/health/metrics", tags=["ops"])
async def metrics() -> dict[str, Any]:
    """进程内计数器快照。生产环境应换成 Prometheus /metrics。"""
    return counter_snapshot()
