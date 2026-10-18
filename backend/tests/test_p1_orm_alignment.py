"""ORM 与 SQL 的一致性检查（纯元数据，不连数据库）。

手写 DDL 最容易脱节的地方：SQL 里加了字段，ORM 忘了改；或者反过来。
这里对比两边定义的表名与列名集合，任一侧多出或少掉都会失败。

**不连库**，所以能在 `make test` 里跑。
"""

from __future__ import annotations

import pytest

from app.db.session import Base
from app.models import (  # noqa: F401  确保所有模型被注册
    Chunk,
    Conversation,
    Document,
    IngestJob,
    KnowledgeBase,
    Message,
    QueryLog,
    UsageDaily,
    User,
    UserLlmConfig,
)

# 与 001_init.sql 保持同步
SQL_TABLES = {
    "users",
    "knowledge_bases",
    "documents",
    "chunks",
    "conversations",
    "messages",
    "ingest_jobs",
    "user_llm_configs",
    "usage_daily",
    "query_logs",
}


class TestOrmAlignment:
    def test_table_set_matches(self) -> None:
        orm_tables = set(Base.metadata.tables.keys())
        assert orm_tables == SQL_TABLES, (
            f"ORM 与 SQL 表不一致\n"
            f"  ORM 多出：{orm_tables - SQL_TABLES}\n"
            f"  SQL 多出：{SQL_TABLES - orm_tables}"
        )

    def test_all_business_tables_have_user_id(self) -> None:
        """除 users 外，每张业务表都必须有 user_id（RLS 的前提）。"""
        for name, table in Base.metadata.tables.items():
            if name == "users":
                continue
            cols = {c.name for c in table.columns}
            assert "user_id" in cols, f"{name} 缺少 user_id 列"

    def test_users_has_username_not_email(self) -> None:
        """D12 决策：用户名登录，不是邮箱。"""
        cols = {c.name for c in User.__table__.columns}
        assert "username" in cols
        assert "email" not in cols

    def test_documents_has_embed_model(self) -> None:
        """D7 决策：记录生成向量的模型标识，用于换模型检测。"""
        cols = {c.name for c in Document.__table__.columns}
        assert "embed_model" in cols

    def test_chunks_has_parent_link(self) -> None:
        """父子块结构：parent_id 自引用。"""
        cols = {c.name for c in Chunk.__table__.columns}
        assert "parent_id" in cols
        assert "level" in cols

    def test_chunks_has_citation_locators(self) -> None:
        """引用定位所需字段：页码 / 工作表 / 行号 / 章节路径。"""
        cols = {c.name for c in Chunk.__table__.columns}
        for required in ("page_from", "page_to", "sheet_name", "row_range", "heading_path"):
            assert required in cols, f"chunks 缺少 {required}"

    def test_messages_has_citations_jsonb(self) -> None:
        cols = {c.name for c in Message.__table__.columns}
        assert "citations" in cols
        assert "trace_id" in cols

    def test_query_logs_has_prompt_version(self) -> None:
        """Prompt 版本化：出问题时能定位当时跑的是哪版。"""
        cols = {c.name for c in QueryLog.__table__.columns}
        assert "prompt_version" in cols

    def test_no_pgvector_import(self) -> None:
        """确保没有引入 pgvector 类型（向量在 Chroma）。"""
        for table in Base.metadata.tables.values():
            for col in table.columns:
                type_name = type(col.type).__name__.lower()
                assert "vector" not in type_name, f"{table.name}.{col.name} 用了向量类型"


class TestModelSemantics:
    @pytest.mark.parametrize(
        "model,expect",
        [
            (User, "users"),
            (KnowledgeBase, "knowledge_bases"),
            (Document, "documents"),
            (Chunk, "chunks"),
            (Conversation, "conversations"),
            (Message, "messages"),
            (IngestJob, "ingest_jobs"),
            (UserLlmConfig, "user_llm_configs"),
            (UsageDaily, "usage_daily"),
            (QueryLog, "query_logs"),
        ],
    )
    def test_tablename(self, model: type, expect: str) -> None:
        assert model.__tablename__ == expect

    def test_ingest_job_status_values(self) -> None:
        """状态机取值必须与 SQL 的 CHECK 约束一致。"""
        ck = [
            c
            for c in IngestJob.__table__.constraints
            if getattr(c, "name", "") == "ck_job_status"
        ]
        assert ck, "ingest_jobs 缺少状态约束"
