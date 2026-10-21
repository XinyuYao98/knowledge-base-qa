/** 知识库与文档列表接口。 */

import { request } from './http'
import type {
  DocumentItem,
  DocumentListResponse,
  Kb,
  KbCreatePayload,
} from '@/types/domain'

export function listKbs(): Promise<Kb[]> {
  return request<Kb[]>('/kbs')
}

export function createKb(payload: KbCreatePayload): Promise<Kb> {
  return request<Kb>('/kbs', { method: 'POST', json: payload })
}

export function getKb(kbId: string): Promise<Kb> {
  return request<Kb>(`/kbs/${kbId}`)
}

/**
 * 删除知识库。
 *
 * 后端要求 `confirm_name` 与库名完全一致才执行 —— 这是防误删的二次校验，
 * 前端要在确认弹窗里让用户手输库名，不能只靠一个「确定」按钮。
 */
export function deleteKb(kbId: string, confirmName: string): Promise<void> {
  return request<void>(`/kbs/${kbId}`, {
    method: 'DELETE',
    json: { confirm_name: confirmName },
  })
}

/**
 * 文档列表。
 *
 * `limit` 后端上限 200（`Query(le=200)`）。前端一页取 50 足够演示，
 * 超过时用 offset 翻页。
 */
export function listDocuments(
  kbId: string,
  params: { limit?: number; offset?: number } = {},
): Promise<DocumentListResponse> {
  const q = new URLSearchParams()
  q.set('limit', String(params.limit ?? 50))
  q.set('offset', String(params.offset ?? 0))
  return request<DocumentListResponse>(`/kbs/${kbId}/documents?${q}`)
}

// ## 注意：删除文档 / 重建索引 后端目前没有端点
//
// 核实过 `backend/app/api/routes/documents.py`，它只有 `POST ""`（上传）一个路由。
// 架构文档 §7.3 里写的「删除、重建索引」在 F3 之前需要后端补接口，
// 前端不能凭空调用不存在的路径。
//
// 已登记到 docs/07-前端技术架构.md 的「后端缺口」。


export type { DocumentItem }
