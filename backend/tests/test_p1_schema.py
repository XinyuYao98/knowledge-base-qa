"""P1 验收：数据库结构与约束（同步连接）。

**这些测试需要真实 PostgreSQL**，默认跳过。
运行方式：`pytest -m db`（需先 `make db-up && make db-init`）

## 为什么用同步 psycopg 而非 ORM/异步

Windows 上 psycopg 异步不支持 ProactorEventLoop。为测试单独绕事件循环，
会引入大量与验证目标无关的复杂度。这里要验证的是**数据库里的结构**，
用同步连接最直接、最可靠。

ORM 与 SQL 的一致性由 `test_p1_orm_alignment.py` 验证（纯元数据比对，不连库）。
"""

from __future__ import annotations

import uuid

import psycopg
import pytest

from app.core.config import settings

pytestmark = pytest.mark.db

EXPECTED_TABLES = {
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

TENANT_TABLES = EXPECTED_TABLES - {"users"}


def _admin_dsn() -> str:
    return settings.DATABASE_ADMIN_URL


def _app_dsn() -> str:
    return settings.DATABASE_URL.replace("postgresql+psycopg://", "postgresql://")


@pytest.fixture(scope="module")
def admin():  # type: ignore[no-untyped-def]
    with psycopg.connect(_admin_dsn(), autocommit=True, connect_timeout=5) as conn:
        yield conn


class TestSchemaStructure:
    def test_all_tables_exist(self, admin) -> None:  # type: ignore[no-untyped-def]
        with admin.cursor() as cur:
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            actual = {r[0] for r in cur.fetchall()}
        missing = EXPECTED_TABLES - actual
        assert not missing, f"缺少表：{missing}"

    def test_rls_enabled_and_forced(self, admin) -> None:  # type: ignore[no-untyped-def]
        """FORCE 是关键：只 ENABLE 时表 owner 仍会绕过策略。"""
        with admin.cursor() as cur:
            cur.execute(
                """
                SELECT relname, relrowsecurity, relforcerowsecurity
                  FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'public' AND c.relkind = 'r'
                   AND relname = ANY(%s)
                """,
                (list(TENANT_TABLES),),
            )
            rows = cur.fetchall()

        assert len(rows) == len(TENANT_TABLES)
        for name, enabled, forced in rows:
            assert enabled, f"{name} 未启用 RLS"
            assert forced, f"{name} 未 FORCE RLS"

    def test_policy_count(self, admin) -> None:  # type: ignore[no-untyped-def]
        with admin.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM pg_policies WHERE policyname = 'p_tenant_isolation'"
            )
            n = cur.fetchone()[0]
        assert n == len(TENANT_TABLES), f"期望 {len(TENANT_TABLES)} 条，实际 {n}"

    def test_pgvector_not_installed(self, admin) -> None:  # type: ignore[no-untyped-def]
        """本项目用 Chroma 存向量，不应启用 pgvector（避免两套向量存储）。"""
        with admin.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_extension WHERE extname = 'vector'")
            assert cur.fetchone()[0] == 0

    def test_auth_functions_are_security_definer(self, admin) -> None:  # type: ignore[no-untyped-def]
        with admin.cursor() as cur:
            cur.execute(
                """
                SELECT proname, prosecdef
                  FROM pg_proc p
                  JOIN pg_namespace n ON n.oid = p.pronamespace
                 WHERE n.nspname = 'public'
                   AND proname IN ('auth_lookup_user', 'auth_get_user')
                """
            )
            rows = cur.fetchall()
        assert len(rows) == 2, "auth_* 函数缺失"
        for name, secdef in rows:
            assert secdef, f"{name} 不是 SECURITY DEFINER"


class TestConstraints:
    def test_kb_name_unique_per_user(self, admin) -> None:  # type: ignore[no-untyped-def]
        """同一用户不能建同名库；不同用户可以同名。"""
        suffix = uuid.uuid4().hex[:6]
        a, b = f"ck_{suffix}_a", f"ck_{suffix}_b"
        with admin.cursor() as cur:
            ids = {}
            for name in (a, b):
                cur.execute(
                    "INSERT INTO users (username, password_hash) VALUES (%s,'x') RETURNING id",
                    (name,),
                )
                ids[name] = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO knowledge_bases (user_id, name, quota_bytes) VALUES (%s,'同名库',%s)",
                    (ids[name], settings.KB_QUOTA_BYTES),
                )

            with pytest.raises(psycopg.errors.UniqueViolation):
                cur.execute(
                    "INSERT INTO knowledge_bases (user_id, name, quota_bytes) VALUES (%s,'同名库',%s)",
                    (ids[a], settings.KB_QUOTA_BYTES),
                )

            for uid in ids.values():
                cur.execute("DELETE FROM users WHERE id = %s", (uid,))

    def test_username_length_check(self, admin) -> None:  # type: ignore[no-untyped-def]
        with admin.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
            cur.execute("INSERT INTO users (username, password_hash) VALUES ('ab','x')")

    def test_quota_check_constraint(self, admin) -> None:  # type: ignore[no-untyped-def]
        name = f"ck_q_{uuid.uuid4().hex[:6]}"
        with admin.cursor() as cur:
            cur.execute(
                "INSERT INTO users (username, password_hash) VALUES (%s,'x') RETURNING id",
                (name,),
            )
            uid = cur.fetchone()[0]
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(
                    "INSERT INTO knowledge_bases (user_id, name, quota_bytes, used_bytes) "
                    "VALUES (%s, '超额库', 100, 200)",
                    (uid,),
                )
            cur.execute("DELETE FROM users WHERE id = %s", (uid,))


class TestApplicationRole:
    def test_not_superuser_and_no_bypassrls(self) -> None:
        with (
            psycopg.connect(_app_dsn(), autocommit=True, connect_timeout=5) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(
                "SELECT current_user, rolsuper, rolbypassrls "
                "FROM pg_roles WHERE rolname = current_user"
            )
            role, is_super, can_bypass = cur.fetchone()
        assert not is_super, f"{role} 是 superuser"
        assert not can_bypass, f"{role} 有 BYPASSRLS"

    def test_cannot_create_table(self) -> None:
        with (
            psycopg.connect(_app_dsn(), autocommit=True, connect_timeout=5) as conn,
            conn.cursor() as cur,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
        ):
            cur.execute("CREATE TABLE should_fail (id int)")
