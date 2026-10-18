-- ============================================================================
-- 002_roles.sql
-- 应用角色的创建与授权。由 bootstrap 脚本用 psycopg 参数化执行，
-- 避免在 SQL 文件里明文写密码。
--
-- 权限最小化原则：
--  - kbapp：只做 DML，不能 DDL
--  - NOBYPASSRLS：最关键的一条，superuser 或 owner 会绕过 RLS
--  - 只授予两个 SECURITY DEFINER 函数的 EXECUTE
-- ============================================================================

-- 说明：下面的 {app_user} / {app_password} 由 bootstrap.py 用参数化命令替换，
-- 本文件仅作为模板与文档留存，不被直接执行。

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{app_user}') THEN
    CREATE ROLE {app_user} LOGIN PASSWORD '{app_password}';
  ELSE
    ALTER ROLE {app_user} WITH LOGIN PASSWORD '{app_password}';
  END IF;
END $$;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {app_user};
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO {app_user};

ALTER ROLE {app_user} NOBYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE;

GRANT EXECUTE ON FUNCTION auth_lookup_user(text) TO {app_user};
GRANT EXECUTE ON FUNCTION auth_get_user(uuid) TO {app_user};
