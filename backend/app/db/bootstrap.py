"""数据库初始化：建表 + RLS + 应用角色。

两种用途：
  1. 开发环境首次初始化：`python -m app.db.bootstrap --with-role`
  2. 生产环境由运维单独执行两个 SQL 文件

它需要 superuser 连接串（DATABASE_ADMIN_URL），与业务连接串（DATABASE_URL）分开，
这样业务进程永远不持有 DDL 权限。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg
from psycopg import sql

from app.core.config import settings

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

ROLE_TEMPLATE = """
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %(app_user)s) THEN
    EXECUTE format('CREATE ROLE %%I LOGIN PASSWORD %%L', %(app_user)s, %(app_password)s);
  ELSE
    EXECUTE format('ALTER ROLE %%I WITH LOGIN PASSWORD %%L', %(app_user)s, %(app_password)s);
  END IF;
END $$;
"""

# 仅授权的纯 DDL 语句：角色名需要标识符转义，用 psycopg.sql 拼接而非常量替换
GRANT_STATEMENTS = [
    "ALTER ROLE {app_user} NOBYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE",
    "GRANT USAGE ON SCHEMA public TO {app_user}",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {app_user}",
    "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
    "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {app_user}",
    "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {app_user}",
    "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {app_user}",
    "GRANT EXECUTE ON FUNCTION auth_lookup_user(text) TO {app_user}",
    "GRANT EXECUTE ON FUNCTION auth_get_user(uuid) TO {app_user}",
]


def _read(name: str) -> str:
    path = MIGRATIONS_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"找不到迁移文件：{path}")
    return path.read_text(encoding="utf-8")


def run_init(with_role: bool, app_user: str, app_password: str | None) -> int:
    admin_url = settings.DATABASE_ADMIN_URL
    if not admin_url:
        print("缺少 DATABASE_ADMIN_URL", file=sys.stderr)
        return 2

    print(f"连接管理库：{_mask_url(admin_url)}")
    with psycopg.connect(admin_url, autocommit=True, connect_timeout=10) as conn, conn.cursor() as cur:
        print("→ 执行 001_init.sql（扩展 / 表 / RLS 策略）")
        cur.execute(_read("001_init.sql"))
        print("  ✓ 完成")

        if with_role:
            if not app_password:
                print("缺少应用角色密码（--password 或环境变量 KB_APP_PASSWORD）", file=sys.stderr)
                return 2
            print(f"→ 创建/更新角色 {app_user}")
            # 不能用 DO $$ ... $$ 里带参数：PL/pgSQL 块内的 $1 无法推断类型。
            # 改为：先查存在性，再发纯 DDL（标识符用 sql.Identifier 转义，密码用 sql.Literal 转义）
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (app_user,))
            exists = cur.fetchone() is not None
            ident = sql.Identifier(app_user)
            pwd = sql.Literal(app_password)
            if exists:
                cur.execute(
                    sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(ident, pwd)
                )
            else:
                cur.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(ident, pwd)
                )
            # 授权语句用 sql.SQL + Identifier 拼接，避免手工 f-string 注入与类型推断问题
            for stmt in GRANT_STATEMENTS:
                cur.execute(sql.SQL(stmt).format(app_user=sql.Identifier(app_user)))
            print("  ✓ 完成（NOBYPASSRLS，仅 DML 权限）")

        # 自检：确认关键约束已生效
        _self_check(cur)
    return 0


def _self_check(cur: psycopg.Cursor) -> None:
    print("\n自检：")
    cur.execute(
        "SELECT count(*) FROM pg_tables WHERE schemaname='public' AND rowsecurity=true"
    )
    n_rls = cur.fetchone()[0]
    print(f"  启用 RLS 的表：{n_rls} 张")

    cur.execute(
        """
        SELECT relname, relrowsecurity, relforcerowsecurity
          FROM pg_class
         WHERE relname IN ('knowledge_bases','documents','chunks','messages')
         ORDER BY relname
        """
    )
    for name, enabled, forced in cur.fetchall():
        mark = "✓" if enabled and forced else "✗"
        print(f"  {mark} {name}: ENABLE={enabled} FORCE={forced}")

    cur.execute("SELECT count(*) FROM pg_policies WHERE policyname='p_tenant_isolation'")
    print(f"  租户隔离策略数量：{cur.fetchone()[0]}（应为 9）")


def _mask_url(url: str) -> str:
    """日志里不打印密码。"""
    import re

    return re.sub(r"(://[^:]+:)[^@]+(@)", r"\1***\2", url)


def main() -> int:
    ap = argparse.ArgumentParser(description="初始化数据库（建表 / RLS / 角色）")
    ap.add_argument("--with-role", action="store_true", help="同时创建或更新应用角色")
    ap.add_argument("--app-user", default="kbapp")
    ap.add_argument("--password", default=None, help="应用角色密码；也可用环境变量 KB_APP_PASSWORD")
    args = ap.parse_args()

    import os

    pwd = args.password or os.environ.get("KB_APP_PASSWORD")
    return run_init(args.with_role, args.app_user, pwd)


if __name__ == "__main__":
    raise SystemExit(main())
