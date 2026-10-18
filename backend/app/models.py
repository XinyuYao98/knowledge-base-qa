"""ORM 模型。

与 `migrations/001_init.sql` 保持一一对应。
改表结构时**两边都要改**：SQL 负责 DDL（含 RLS），ORM 负责类型映射。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("char_length(username) BETWEEN 3 AND 20", name="ck_users_username_len"),
    )

    id: Mapped[uuid.UUID] = _pk()
    username: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = _created()


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_kb_user_name"),
        CheckConstraint("used_bytes >= 0 AND used_bytes <= quota_bytes", name="ck_kb_quota"),
        Index("ix_kb_user", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    quota_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    used_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    doc_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("kb_id", "sha256", name="uq_doc_kb_sha"),
        Index("ix_doc_kb_created", "kb_id", "created_at"),
        Index("ix_doc_user_status", "user_id", "status"),
    )

    id: Mapped[uuid.UUID] = _pk()
    kb_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    mime: Mapped[str] = mapped_column(String(128), nullable=False)
    ext: Mapped[str] = mapped_column(String(16), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    # pending|parsing|chunking|embedding|ready|failed|unsupported
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    parse_meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    embed_model: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    created_at: Mapped[datetime] = _created()


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        CheckConstraint("level IN ('parent','child')", name="ck_chunk_level"),
        Index("ix_chunk_doc_seq", "document_id", "seq"),
        Index("ix_chunk_kb_level", "kb_id", "level"),
    )

    id: Mapped[uuid.UUID] = _pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    kb_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chunks.id", ondelete="CASCADE"), nullable=True
    )
    level: Mapped[str] = mapped_column(String(8), default="child", nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_type: Mapped[str] = mapped_column(String(16), default="text", nullable=False)
    heading_path: Mapped[str] = mapped_column(Text, default="", nullable=False)
    page_from: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_to: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sheet_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    row_range: Mapped[str | None] = mapped_column(String(32), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = _created()


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conv_kb_created", "kb_id", "created_at"),)

    id: Mapped[uuid.UUID] = _pk()
    kb_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    title: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    created_at: Mapped[datetime] = _created()


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint("role IN ('user','assistant')", name="ck_msg_role"),
        Index("ix_msg_conv_created", "conversation_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, nullable=False)
    trace_id: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    model: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    flags: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = _created()


class IngestJob(Base):
    __tablename__ = "ingest_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','retrying','waiting_model','done','failed')",
            name="ck_job_status",
        ),
        Index("ix_job_pickup", "status", "run_after"),
        Index("ix_job_doc", "document_id"),
    )

    id: Mapped[uuid.UUID] = _pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)
    stage: Mapped[str] = mapped_column(String(16), default="parse", nullable=False)
    attempts: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class UserLlmConfig(Base):
    __tablename__ = "user_llm_configs"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    api_key_cipher: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    extra: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    last_check_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_check_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class UsageDaily(Base):
    __tablename__ = "usage_daily"

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    ask_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)


class QueryLog(Base):
    __tablename__ = "query_logs"
    __table_args__ = (
        Index("ix_qlog_user_created", "user_id", "created_at"),
        Index("ix_qlog_trace", "trace_id"),
    )

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kb_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(32), nullable=False)
    question_chars: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    retrieve_hits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    top_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    used_context_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    first_token_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    citation_valid: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    repair_attempted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    grounded: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    rewrite_triggered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="ok", nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(16), default="", nullable=False)
    embed_model: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    created_at: Mapped[datetime] = _created()


__all__ = [
    "Chunk",
    "Conversation",
    "Document",
    "IngestJob",
    "KnowledgeBase",
    "Message",
    "QueryLog",
    "UsageDaily",
    "User",
    "UserLlmConfig",
]
