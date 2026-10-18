-- ============================================================================
-- 001_init.sql
-- 需以 postgres superuser 执行：python -m app.db.bootstrap
--
-- 设计要点：
--  1. 所有业务表带 user_id，并启用 FORCE ROW LEVEL SECURITY
--  2. RLS 策略统一为 user_id = current_setting('app.user_id', true)::uuid
--     —— 未设置时返回 NULL，查询结果为空（安全默认值）
--  3. 每张表都有 created_at 便于排查与归档
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------------
-- 用户
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    username      varchar(20) NOT NULL,
    password_hash text NOT NULL,
    is_active     boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_users_username UNIQUE (username),
    CONSTRAINT ck_users_username_len CHECK (char_length(username) BETWEEN 3 AND 20)
);

-- ---------------------------------------------------------------------------
-- 知识库
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_bases (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        varchar(100) NOT NULL,
    description text NOT NULL DEFAULT '',
    status      varchar(20) NOT NULL DEFAULT 'active',
    quota_bytes bigint NOT NULL,
    used_bytes  bigint NOT NULL DEFAULT 0,
    doc_count   integer NOT NULL DEFAULT 0,
    chunk_count integer NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    -- 命名规则（D8-b）：入库前 strip()，大小写敏感
    CONSTRAINT uq_kb_user_name UNIQUE (user_id, name),
    CONSTRAINT ck_kb_quota CHECK (used_bytes >= 0 AND used_bytes <= quota_bytes)
);
CREATE INDEX IF NOT EXISTS ix_kb_user ON knowledge_bases (user_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- 文档
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS documents (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kb_id        uuid NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    user_id      uuid NOT NULL,
    filename     varchar(512) NOT NULL,
    mime         varchar(128) NOT NULL,
    ext          varchar(16) NOT NULL,
    size_bytes   bigint NOT NULL,
    sha256       varchar(64) NOT NULL,
    storage_key  varchar(512) NOT NULL DEFAULT '',
    status       varchar(20) NOT NULL DEFAULT 'pending',
    error_code   varchar(64),
    error_detail text,
    parse_meta   jsonb NOT NULL DEFAULT '{}'::jsonb,
    chunk_count  integer NOT NULL DEFAULT 0,
    embed_model  varchar(128) NOT NULL DEFAULT '',
    created_at   timestamptz NOT NULL DEFAULT now(),
    -- 秒传与幂等兜底
    CONSTRAINT uq_doc_kb_sha UNIQUE (kb_id, sha256)
);
CREATE INDEX IF NOT EXISTS ix_doc_kb_created ON documents (kb_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_doc_user_status ON documents (user_id, status);

-- ---------------------------------------------------------------------------
-- 切块（父子块）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chunks (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id  uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    kb_id        uuid NOT NULL,
    user_id      uuid NOT NULL,
    parent_id    uuid REFERENCES chunks(id) ON DELETE CASCADE,
    level        varchar(8) NOT NULL DEFAULT 'child',
    seq          integer NOT NULL,
    chunk_type   varchar(16) NOT NULL DEFAULT 'text',
    heading_path text NOT NULL DEFAULT '',
    page_from    integer,
    page_to      integer,
    sheet_name   varchar(128),
    row_range    varchar(32),
    text         text NOT NULL,
    token_count  integer NOT NULL DEFAULT 0,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_chunk_level CHECK (level IN ('parent', 'child'))
);
CREATE INDEX IF NOT EXISTS ix_chunk_doc_seq ON chunks (document_id, seq);
CREATE INDEX IF NOT EXISTS ix_chunk_kb_level ON chunks (kb_id, level);
CREATE INDEX IF NOT EXISTS ix_chunk_parent ON chunks (parent_id) WHERE parent_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 会话与消息
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS conversations (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kb_id      uuid NOT NULL REFERENCES knowledge_bases(id) ON DELETE CASCADE,
    user_id    uuid NOT NULL,
    title      varchar(200) NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_conv_kb_created ON conversations (kb_id, created_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id   uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    user_id           uuid NOT NULL,
    role              varchar(16) NOT NULL,
    content           text NOT NULL DEFAULT '',
    citations         jsonb NOT NULL DEFAULT '[]'::jsonb,
    trace_id          varchar(32) NOT NULL DEFAULT '',
    model             varchar(128) NOT NULL DEFAULT '',
    prompt_tokens     integer NOT NULL DEFAULT 0,
    completion_tokens integer NOT NULL DEFAULT 0,
    latency_ms        integer NOT NULL DEFAULT 0,
    flags             jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_msg_role CHECK (role IN ('user', 'assistant'))
);
CREATE INDEX IF NOT EXISTS ix_msg_conv_created ON messages (conversation_id, created_at);

-- ---------------------------------------------------------------------------
-- 索引任务队列
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ingest_jobs (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    user_id     uuid NOT NULL,
    status      varchar(16) NOT NULL DEFAULT 'queued',
    stage       varchar(16) NOT NULL DEFAULT 'parse',
    attempts    smallint NOT NULL DEFAULT 0,
    last_error  text,
    run_after   timestamptz NOT NULL DEFAULT now(),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_job_status CHECK (
        status IN ('queued', 'running', 'retrying', 'waiting_model', 'done', 'failed')
    )
);
CREATE INDEX IF NOT EXISTS ix_job_pickup ON ingest_jobs (status, run_after)
    WHERE status IN ('queued', 'retrying', 'waiting_model');
CREATE INDEX IF NOT EXISTS ix_job_doc ON ingest_jobs (document_id);

-- ---------------------------------------------------------------------------
-- 用户模型配置
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_llm_configs (
    user_id          uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    base_url         varchar(512) NOT NULL,
    api_key_cipher   text NOT NULL,
    model            varchar(128) NOT NULL,
    extra            jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_check_ok    boolean,
    last_check_at    timestamptz,
    last_check_error text,
    updated_at       timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 用量与埋点
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS usage_daily (
    user_id           uuid NOT NULL,
    day               varchar(10) NOT NULL,
    ask_count         integer NOT NULL DEFAULT 0,
    prompt_tokens     bigint NOT NULL DEFAULT 0,
    completion_tokens bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);

CREATE TABLE IF NOT EXISTS query_logs (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             uuid NOT NULL,
    kb_id               uuid NOT NULL,
    trace_id            varchar(32) NOT NULL,
    question_chars      integer NOT NULL DEFAULT 0,
    retrieve_hits       integer NOT NULL DEFAULT 0,
    top_score           double precision,
    used_context_tokens integer NOT NULL DEFAULT 0,
    prompt_tokens       integer NOT NULL DEFAULT 0,
    completion_tokens   integer NOT NULL DEFAULT 0,
    latency_ms          integer NOT NULL DEFAULT 0,
    first_token_ms      integer,
    citation_valid      boolean NOT NULL DEFAULT true,
    repair_attempted    boolean NOT NULL DEFAULT false,
    grounded            boolean NOT NULL DEFAULT true,
    rewrite_triggered   boolean NOT NULL DEFAULT false,
    status              varchar(32) NOT NULL DEFAULT 'ok',
    prompt_version      varchar(16) NOT NULL DEFAULT '',
    embed_model         varchar(128) NOT NULL DEFAULT '',
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_qlog_user_created ON query_logs (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_qlog_trace ON query_logs (trace_id);

-- ============================================================================
-- 行级安全（RLS）
-- ============================================================================
-- 统一策略：user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
-- 未设置时 current_setting 返回 NULL → 比较结果 NULL → 行不可见。
-- 这是「忘记设置就什么都查不到」的安全默认值。

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'knowledge_bases','documents','chunks',
    'conversations','messages','ingest_jobs',
    'user_llm_configs','usage_daily','query_logs'
  ] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    -- FORCE：连表 owner 也受策略约束（owner 默认会绕过 RLS）
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS p_tenant_isolation ON %I', t);
    EXECUTE format($f$
      CREATE POLICY p_tenant_isolation ON %I
        USING (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
        WITH CHECK (user_id = NULLIF(current_setting('app.user_id', true), '')::uuid)
    $f$, t);
  END LOOP;
END $$;

-- users 表只允许查自己（登录走 SECURITY DEFINER 函数，见 002）
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE users FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_users_self ON users;
CREATE POLICY p_users_self ON users
  USING (id = NULLIF(current_setting('app.user_id', true), '')::uuid);

-- ============================================================================
-- 登录/注册所需的最小跨租户读（SECURITY DEFINER，而非给角色开 BYPASSRLS）
-- ============================================================================

CREATE OR REPLACE FUNCTION auth_lookup_user(p_username text)
RETURNS TABLE (id uuid, username varchar, password_hash text, is_active boolean, created_at timestamptz)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT u.id, u.username, u.password_hash, u.is_active, u.created_at
    FROM users u
   WHERE u.username = p_username
   LIMIT 1;
$$;

CREATE OR REPLACE FUNCTION auth_get_user(p_id uuid)
RETURNS TABLE (id uuid, username varchar, created_at timestamptz, is_active boolean)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT u.id, u.username, u.created_at, u.is_active
    FROM users u
   WHERE u.id = p_id
   LIMIT 1;
$$;

REVOKE ALL ON FUNCTION auth_lookup_user(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION auth_get_user(uuid) FROM PUBLIC;
