"""应用配置。

所有可调参数集中在此，禁止在业务代码里散落魔法数字。
取值优先级：环境变量 > .env 文件 > 本文件的默认值。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录（backend 的上一级）
ROOT_DIR = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(ROOT_DIR / ".env", ROOT_DIR / ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------
    APP_ENV: Literal["dev", "staging", "prod"] = "dev"
    LOG_LEVEL: str = "INFO"
    API_PREFIX: str = "/api/v1"
    CORS_ORIGINS: str = "http://localhost:3000"

    # ------------------------------------------------------------------
    # 数据库
    # ------------------------------------------------------------------
    # 业务连接串：必须是非 superuser 角色，否则 RLS 被绕过
    DATABASE_URL: str = "postgresql+psycopg://kbapp:kbapp_dev_pwd@127.0.0.1:5432/kbqa"
    # 管理连接串：仅用于建表建角色，不参与业务
    DATABASE_ADMIN_URL: str = "postgresql://postgres:postgres_dev_pwd@127.0.0.1:5432/kbqa"

    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_TIMEOUT: float = 5.0
    DB_CONNECT_TIMEOUT: int = 5
    DB_POOL_RECYCLE: int = 1800

    # ------------------------------------------------------------------
    # Redis
    # ------------------------------------------------------------------
    REDIS_URL: str = "redis://127.0.0.1:6379/0"
    REDIS_SOCKET_TIMEOUT: float = 3.0

    # ------------------------------------------------------------------
    # Chroma
    # ------------------------------------------------------------------
    # embedded：进程内持久化（单机最简，但多进程不共享）
    # http：独立服务（api 与 worker 共享，生产必须用这个）
    CHROMA_MODE: Literal["embedded", "http"] = "embedded"
    CHROMA_HOST: str = "127.0.0.1"
    CHROMA_PORT: int = 8000
    CHROMA_PATH: str = "./data/chroma"
    CHROMA_TIMEOUT_SECONDS: float = 10.0

    # ------------------------------------------------------------------
    # 文件存储
    # ------------------------------------------------------------------
    STORAGE_ROOT: str = "./data"

    # ------------------------------------------------------------------
    # 安全
    # ------------------------------------------------------------------
    JWT_SECRET: str = Field(default="dev-only-change-me-in-production-please")
    JWT_ALG: str = "HS256"
    ACCESS_TOKEN_TTL_SECONDS: int = 900
    REFRESH_TOKEN_TTL_SECONDS: int = 604800
    FERNET_KEY: str = ""

    # ------------------------------------------------------------------
    # Embedding 服务（本地，OpenAI 兼容协议）
    # ------------------------------------------------------------------
    EMBED_BASE_URL: str = "http://127.0.0.1:1234/v1"
    EMBED_API_KEY: str = "lm-studio"
    EMBED_MODEL: str = "qwen3-embedding-4b"
    # 必须与库中已有向量一致；换值后须 make reindex
    EMBED_DIM: int = 2560
    # LM Studio 通常不支持 dimensions 参数；vLLM 需 --hf-overrides 开启 MRL
    EMBED_SEND_DIMENSIONS: bool = False
    EMBED_BATCH_SIZE: int = 16
    EMBED_TIMEOUT_SECONDS: float = 60.0
    EMBED_CONCURRENCY: int = 2
    # Qwen3-Embedding 是「指令感知」模型：query 侧带指令，document 侧不带
    EMBED_QUERY_INSTRUCTION: str = (
        "Given a web search query, retrieve relevant passages that answer the query"
    )

    # ------------------------------------------------------------------
    # Reranker 服务（本地，OpenAI 兼容 /v1/completions 或专用接口）
    # ------------------------------------------------------------------
    RERANK_ENABLED: bool = True
    RERANK_BASE_URL: str = "http://127.0.0.1:1234/v1"
    RERANK_API_KEY: str = "lm-studio"
    RERANK_MODEL: str = "qwen3-reranker-0.6b"
    RERANK_TIMEOUT_SECONDS: float = 5.0
    RERANK_TOP_N: int = 5

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------
    RRF_K: int = 60
    RETRIEVE_VECTOR_TOP_K: int = 20
    RETRIEVE_BM25_TOP_K: int = 20
    RETRIEVE_FINAL_TOP_K: int = 5
    RETRIEVE_MIN_SCORE: float = 0.15
    RETRIEVAL_TIMEOUT_SECONDS: float = 5.0
    ENABLE_KEYWORD_CHANNEL: bool = True

    # ------------------------------------------------------------------
    # 切块
    # ------------------------------------------------------------------
    CHUNK_TARGET_TOKENS: int = 400
    CHUNK_OVERLAP_TOKENS: int = 60
    PARENT_TARGET_TOKENS: int = 1500
    MIN_CHUNK_CHARS: int = 20

    # ------------------------------------------------------------------
    # 生成
    # ------------------------------------------------------------------
    CHAT_CONTEXT_MAX_TOKENS: int = 6000
    CHAT_MAX_OUTPUT_TOKENS: int = 1200
    CHAT_TEMPERATURE: float = 0.2
    CHAT_FIRST_TOKEN_TIMEOUT_SECONDS: float = 20.0
    CHAT_TOTAL_TIMEOUT_SECONDS: float = 120.0
    CHAT_MAX_QUESTION_CHARS: int = 4000
    CHAT_HISTORY_TURNS: int = 6
    # 引用校验失败后的修复次数，硬上限 1
    MAX_REPAIR_ATTEMPTS: int = 1
    # 查询改写（指代消解）超时
    REWRITE_TIMEOUT_SECONDS: float = 5.0

    # ------------------------------------------------------------------
    # 配额（产品规则，写死在代码里，不做运营后台）
    # ------------------------------------------------------------------
    MAX_KB_PER_USER: int = 5
    KB_QUOTA_BYTES: int = 1 * 1024 * 1024 * 1024      # 1 GiB
    MAX_SINGLE_FILE_BYTES: int = 200 * 1024 * 1024    # 200 MB
    MAX_FILES_PER_KB: int = 1000
    DAILY_ASK_LIMIT: int = 200
    MAX_CONCURRENT_ASK: int = 3
    MAX_CONCURRENT_INGEST: int = 2
    UPLOAD_PART_SIZE: int = 5 * 1024 * 1024           # 5 MB
    UPLOAD_SESSION_TTL_SECONDS: int = 86400

    # ------------------------------------------------------------------
    # 限流
    # ------------------------------------------------------------------
    RATE_ASK_PER_MINUTE: int = 20
    RATE_UPLOAD_INIT_PER_HOUR: int = 60
    RATE_LOGIN_PER_MINUTE: int = 5
    RATE_REGISTER_PER_HOUR: int = 5

    # ------------------------------------------------------------------
    # 数据保留（见架构文档 §6.3）
    # ------------------------------------------------------------------
    STAGING_RETENTION_DAYS: int = 7
    UPLOAD_PART_RETENTION_HOURS: int = 24

    # ------------------------------------------------------------------
    # 索引任务
    # ------------------------------------------------------------------
    INGEST_MAX_ATTEMPTS: int = 3
    INGEST_JOB_TIMEOUT_SECONDS: int = 30 * 60
    INGEST_STALE_MINUTES: int = 30
    INGEST_POLL_SECONDS: float = 2.0

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------
    PDF_SCANNED_TEXT_PER_PAGE: int = 50      # 每页可提取字符低于此判定为扫描件

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------
    @field_validator("JWT_SECRET")
    @classmethod
    def _secret_must_change(cls, v: str) -> str:
        if len(v) < 16:
            raise ValueError("JWT_SECRET 至少 16 字符")
        return v

    @field_validator("EMBED_DIM")
    @classmethod
    def _dim_sane(cls, v: int) -> int:
        if v not in (512, 768, 1024, 1536, 2048, 2560, 4096):
            raise ValueError(f"EMBED_DIM={v} 不常见，请确认模型输出维度")
        return v

    @field_validator("MAX_REPAIR_ATTEMPTS")
    @classmethod
    def _repair_bounded(cls, v: int) -> int:
        # 架构约束：修复最多 1 次，禁止无限循环
        if v > 1:
            raise ValueError("MAX_REPAIR_ATTEMPTS 不得超过 1（架构硬约束 C5）")
        return v

    @model_validator(mode="after")
    def _prod_checks(self) -> Settings:
        if self.APP_ENV == "prod":
            if "dev-only" in self.JWT_SECRET:
                raise ValueError("生产环境必须设置自定义 JWT_SECRET")
            if not self.FERNET_KEY:
                raise ValueError("生产环境必须设置 FERNET_KEY（用于加密用户模型密钥）")
            if self.CHROMA_MODE != "http":
                raise ValueError("生产环境 CHROMA_MODE 必须为 http，否则多进程无法共享向量库")
        return self

    # ------------------------------------------------------------------
    # 派生属性
    # ------------------------------------------------------------------
    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def storage_root(self) -> Path:
        return Path(self.STORAGE_ROOT).resolve()

    @property
    def files_dir(self) -> Path:
        return self.storage_root / "files"

    @property
    def staging_dir(self) -> Path:
        return self.storage_root / "staging"

    @property
    def uploads_dir(self) -> Path:
        return self.storage_root / "uploads"

    @property
    def bm25_dir(self) -> Path:
        return self.storage_root / "bm25"

    def ensure_dirs(self) -> None:
        for d in (self.files_dir, self.staging_dir, self.uploads_dir, self.bm25_dir):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
