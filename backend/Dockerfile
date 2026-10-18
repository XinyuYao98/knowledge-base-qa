# syntax=docker/dockerfile:1.7

# ---------------------------------------------------------------------------
# 后端镜像
# 说明：不包含模型权重。模型服务由用户在本机启动（见 README 启动顺序）。
#      这样镜像保持在 1GB 以内，且不承担模型分发义务。
# ---------------------------------------------------------------------------

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

# libpq: psycopg 运行时需要
# libxml2/libxslt: lxml 运行时需要
# curl: 健康检查
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        libxml2 \
        libxslt1.1 \
        curl \
        tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖单独一层，代码变动不必重装依赖
COPY pyproject.toml ./
RUN pip install --upgrade pip setuptools wheel \
    && pip install .

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini

# 非 root 运行
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data && chown -R appuser:appuser /data /app
USER appuser

ENV STORAGE_ROOT=/data

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "2"]
