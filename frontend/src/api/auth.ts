/** 认证接口。 */

import { request } from './http'
import type { LoginPayload, RegisterPayload, TokenResponse, User } from '@/types/domain'

/**
 * 注册。
 *
 * `skipAuthRedirect` 是必要的：注册失败（如用户名已存在）可能返回 401/409，
 * 如果走全局 401 跳转，用户会被踢回登录页而不是看到具体错误。
 */
export function register(payload: RegisterPayload): Promise<TokenResponse> {
  return request<TokenResponse>('/auth/register', {
    method: 'POST',
    json: payload,
    skipAuthRedirect: true,
  })
}

export function login(payload: LoginPayload): Promise<TokenResponse> {
  return request<TokenResponse>('/auth/login', {
    method: 'POST',
    json: payload,
    skipAuthRedirect: true,
  })
}

/** 当前用户。用于刷新页面后确认 token 还有效。 */
export function fetchMe(): Promise<User> {
  return request<User>('/auth/me')
}
