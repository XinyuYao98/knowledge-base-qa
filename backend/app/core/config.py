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
    # 索引 Worker
    # ------------------------------------------------------------------
    #: 一个 worker 进程内并发处理的任务数。
    #: 不要设太大：embedding 服务本身并发能力有限，压太多反而排队。
    WORKER_CONCURRENCY: int = 2
    #: 队列为空时的轮询间隔（秒）
    WORKER_POLL_INTERVAL: float = 2.0
    #: 单文档索引超时（秒）。403 块的 PDF 实测约 2 分钟，留足余量。
    INDEX_TIMEOUT_SECONDS: float = 600.0

    # ------------------------------------------------------------------
    # 安全
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # JWT 与认证（P5）
    # ------------------------------------------------------------------
    #: P5 决策 A：24 小时、无 refresh token。
    #: 演示场景够用；refresh 要多「刷新端点 + 存储 + 撤销」一整套。
    JWT_ACCESS_TTL_HOURS: int = 24

    # ------------------------------------------------------------------
    # 限流（Redis 固定窗口）
    # ------------------------------------------------------------------
    RATE_LIMIT_LOGIN_PER_MINUTE: int = 10        # 按 IP
    RATE_LIMIT_LOGIN_USER_ATTEMPTS: int = 5      # 按用户名
    RATE_LIMIT_LOGIN_USER_WINDOW_SECONDS: int = 900   # 15 分钟
    RATE_LIMIT_REGISTER_PER_HOUR: int = 5

    # ------------------------------------------------------------------
    # 检索（P6）
    # ------------------------------------------------------------------
    #: 两路召回各取多少（用户决策：Top 20）
    RETRIEVE_VECTOR_TOP_K: int = 20
    RETRIEVE_BM25_TOP_K: int = 20
    #: RRF 融合后送给 reranker 的条数（架构 D1）
    RETRIEVE_FUSE_TOP_K: int = 20
    #: rerank 后最终进上下文的条数（架构 D1 v3.1：从 8 改为 5）
    RETRIEVE_FINAL_TOP_K: int = 5
    #: BM25 快照防抖落盘间隔（秒）
    BM25_SAVE_DEBOUNCE_SECONDS: float = 5.0

    # ------------------------------------------------------------------
    # 查询改写（D13）
    # ------------------------------------------------------------------
    REWRITE_MAX_TOKENS: int = 128
    #: 答案生成的最大 token。500 字左右的答案够用
    ANSWER_MAX_TOKENS: int = 900
    #: 送进提示词的资料总字符上限。
    #: 架构 §D1：5 条资料约 4000 token，折合中文约 6000 字符
    CONTEXT_MAX_CHARS: int = 6000
    REWRITE_TIMEOUT_SECONDS: float = 5.0

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
    # 以服务端 /v1/models 返回的 id 为准，可能带 text-embedding- 前缀
    EMBED_MODEL: str = "text-embedding-qwen3-embedding-4b"
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
    # 生成模型（用户自带，OpenAI 兼容）
    # ------------------------------------------------------------------
    CHAT_BASE_URL: str = "http://127.0.0.1:1234/v1"
    CHAT_MODEL: str = "qwen/qwen3.5-9b"
    CHAT_API_KEY: str = "lm-studio"

    # ------------------------------------------------------------------
    # 本地模型服务必须绕开系统代理
    # ------------------------------------------------------------------
    # 实测：若系统设置了 HTTP_PROXY，httpx 默认会把 127.0.0.1 的请求
    # 也转发给代理，导致 ReadTimeout。本地服务一律不走代理。
    LLM_DISABLE_PROXY: bool = True

    # ------------------------------------------------------------------
    # Reranker 服务（本地，OpenAI 兼容 /v1/completions 或专用接口）
    # ------------------------------------------------------------------
    #: 默认开启。
    #:
    #: ## 109 条评测的结论
    #:
    #: | 配置 | 通过率 | p50 |
    #: |---|---|---|
    #: | 开 rerank | 93/109 = 85.3% | 9.2s |
    #: | 关 rerank | 86/109 = 78.9% | 1.0s |
    #:
    #: 净 +7 条（+6.4%），代价是慢 9 倍。
    #:
    #: ## 但它其实是「创可贴」
    #:
    #: rerank 救活的 13 条和搞坏的 6 条**全是表格取数题**。
    #: 根因是 P3 的表格行切块丢了列头和项目名：
    #:
    #:     seq=26  「的冲销部分 | | 616,631.23 |」   ← 项目名被截断
    #:
    #: 同一份数据在 text 块里是完整的，在 table_row 块里是残缺的。
    #: rerank 的作用不是「排序更准」，而是「更倾向挑完整的块」。
    #:
    #: **修好表格切块后，这 13 条收益会消失。**
    #: 那时应该重新评测，再决定是否继续开启。
    #:
    #: ## 一个早期结论被推翻了
    #:
    #: 26 条时测得「关掉更好」，那是错的 —— 26 条里 1 条差异 = 3.8%，
    #: 且稳定性实验显示单次评测有约 7% 的不确定性。
    RERANK_ENABLED: bool = True
    RERANK_BASE_URL: str = "http://127.0.0.1:1234/v1"
    RERANK_API_KEY: str = "lm-studio"
    RERANK_MODEL: str = "qwen3-reranker-0.6b"
    RERANK_TIMEOUT_SECONDS: float = 5.0
    RERANK_TOP_N: int = 5
    #: 调用协议：
    #: - auto：先试 /v1/rerank，失败回退 chat 打分（推荐，兼容性最好）
    #: - endpoint：只用 /v1/rerank（vLLM / TEI 等支持该协议的服务）
    #: - chat：只用 chat + 提示词打分（LM Studio 的常见形态）
    RERANK_PROTOCOL: Literal["auto", "endpoint", "chat"] = "auto"
    #: chat 打分时的并发数。逐对串行在 20 条候选下太慢。
    RERANK_CONCURRENCY: int = 4

    # ------------------------------------------------------------------
    # 检索（补充项）
    # ------------------------------------------------------------------
    # 注意：RETRIEVE_* 的 top-k 定义在上面的「检索（P6）」段，
    # 这里只保留 P1 时代就有的、P6 没用到的参数。
    # **不要再重复定义同名项** —— P7 清理时发现 VECTOR_TOP_K 被定义了两次，
    # 改上面那个不生效（pydantic 以后定义的为准），排查了半小时。
    RRF_K: int = 60
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
        """存储根目录，**始终相对项目根解析**。

        不能直接 `Path(STORAGE_ROOT).resolve()`：
        `./data` 会按当前工作目录解析，导致在 `backend/` 下跑脚本时
        指到 `backend/data`，与在项目根跑时的 `项目/data` 变成两套数据。

        实测踩过：管道在项目根写入，读质量报告时在 backend 下读不到。

        规则：
        - 绝对路径 → 原样使用
        - 相对路径 → 相对项目根（ROOT_DIR）
        """
        p = Path(self.STORAGE_ROOT)
        return p.resolve() if p.is_absolute() else (ROOT_DIR / p).resolve()

    @property
    def chroma_path(self) -> Path:
        """Chroma 持久化目录。同 storage_root，相对路径按项目根解析。"""
        p = Path(self.CHROMA_PATH)
        return p.resolve() if p.is_absolute() else (ROOT_DIR / p).resolve()

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
