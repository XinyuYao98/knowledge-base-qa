-- ============================================================================
-- P5：认证相关函数
-- ============================================================================
--
-- 为什么需要这个迁移
--
-- `users` 表受 RLS 保护，策略是：
--
--     USING (id = current_setting('app.user_id')::uuid)
--
-- 应用角色（kbapp）执行 `INSERT INTO users` 时 `app.user_id` 为空，
-- 策略判定为 false，直接报：
--
--     InsufficientPrivilege: new row violates row-level security policy
--
-- P4 阶段实测确认过。所以注册必须走 SECURITY DEFINER 函数。
--
-- 为什么不是「给 API 一个管理员连接」
--
-- 那等于让 Web 进程持有 superuser 连接 —— 一旦有 SQL 注入，
-- 整库沦陷。而 SECURITY DEFINER 只把提权限制在
-- 「一个只能插入 users 行的函数」里。
--
-- 安全要点
--
-- 1. `SET search_path = public`：防 search_path 劫持
-- 2. `REVOKE ALL FROM PUBLIC`：只有显式 GRANT 的角色能调用
-- 3. 函数内捕获唯一冲突，返回 NULL 而不是抛异常 ——
--    让上层能用「用户名已存在」这种业务错误回应，而不是 500
-- ============================================================================

CREATE OR REPLACE FUNCTION auth_register(p_username text, p_password_hash text)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
  v_id uuid;
BEGIN
  INSERT INTO users (username, password_hash)
  VALUES (p_username, p_password_hash)
  RETURNING id INTO v_id;

  RETURN v_id;
EXCEPTION
  WHEN unique_violation THEN
    -- 用户名已存在：返回 NULL，由调用方转成 409
    RETURN NULL;
END;
$$;

REVOKE ALL ON FUNCTION auth_register(text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION auth_register(text, text) TO {app_user};

-- ---------------------------------------------------------------------------
-- 登录失败计数（限流用 PG 兜底，Redis 不可降级用）
-- ---------------------------------------------------------------------------
-- 说明：主限流走 Redis（见 app/core/ratelimit.py）。
-- 这张表不参与正常路径，只在需要审计「谁在被爆破」时用。
CREATE TABLE IF NOT EXISTS login_attempts (
    id          bigserial PRIMARY KEY,
    username    varchar(64) NOT NULL,
    ip          varchar(64) NOT NULL DEFAULT '',
    success     boolean NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_login_attempts_user_time
    ON login_attempts (username, created_at DESC);

-- 只允许追加，不允许读（防止被用来枚举用户名）
ALTER TABLE login_attempts ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_login_attempts_insert ON login_attempts FOR INSERT WITH CHECK (true);
GRANT INSERT ON login_attempts TO {app_user};
GRANT USAGE ON SEQUENCE login_attempts_id_seq TO {app_user};
