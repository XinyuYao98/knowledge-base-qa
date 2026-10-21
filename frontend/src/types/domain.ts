/**
 * 领域类型（手写部分）。
 *
 * ## 为什么不全靠 openapi-typescript 生成
 *
 * 生成的 `generated.d.ts` 描述的是 **HTTP 层**（`paths` / `components.schemas`），
 * 用法是 `paths['/kbs']['get']['responses']['200']` 这种长路径，
 * 在组件里写起来很啰嗦。
 *
 * 所以这里做一层**薄的语义化别名**：手写，但字段全部来自后端代码核对，
 * 不凭印象。生成类型仍然保留，用于「后端改了字段，这里编译报错」的兜底。
 *
 * 核对来源（2026-10-21）：
 *   KbOut       → backend/app/api/routes/kbs.py:44
 *   KbCreate    → backend/app/api/routes/kbs.py:35
 *   文档列表     → backend/app/api/routes/kbs.py:211
 *   Register... → backend/app/api/routes/auth.py:32
 */

export interface Kb {
  id: string
  name: string
  description: string
  doc_count: number
  chunk_count: number
  used_bytes: number
  quota_bytes: number
  created_at: string
}

export interface KbCreatePayload {
  name: string
  /** 后端 max_length=500 */
  description?: string
}

/**
 * 文档索引状态。
 *
 * 后端没有用枚举类，值是字符串。这里按 worker 流水线的实际取值列出：
 * `queued` / `parsing` / `chunking` / `embedding` / `ready` / `failed`
 *
 * **注意**：中间态的具体名字要以 worker 为准，页面里不要写死判断，
 * 用 `isProcessing(status)` 这类辅助函数，加新状态时只改一处。
 */
export type DocumentStatus =
  | 'queued'
  | 'parsing'
  | 'chunking'
  | 'embedding'
  | 'ready'
  | 'failed'
  | string

export interface DocumentItem {
  id: string
  filename: string
  ext: string
  size_bytes: number
  status: DocumentStatus
  chunk_count: number
  error_code: string | null
  error_detail: string | null
  created_at: string
}

export interface DocumentListResponse {
  total: number
  items: DocumentItem[]
}

export interface User {
  id: string
  username: string
  created_at: string
}

export interface TokenResponse {
  access_token: string
  token_type: string
  expires_in: number
}

export interface RegisterPayload {
  /** 后端 3–32，DB 约束 3–20 —— 前端按 3–20 校验（见架构文档 §7.1） */
  username: string
  /** 后端 8–128 */
  password: string
}

export interface LoginPayload {
  username: string
  password: string
}

/** 中间态判定：这些状态需要前端轮询 */
export function isProcessing(status: DocumentStatus): boolean {
  return status !== 'ready' && status !== 'failed'
}
