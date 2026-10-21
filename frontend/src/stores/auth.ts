/**
 * 认证状态。
 *
 * ## 只存两样东西
 *
 * `token` 和 `user`。库列表、文档列表、消息记录都不在这里 ——
 * 它们是页面级数据，用组件内的 `ref` 即可（见架构 §五）。
 *
 * 放进 store 的代价是「什么时候该清空」变成一个新问题，
 * 而这个应用没有跨页面共享列表数据的需求。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { fetchMe, login as loginApi, register as registerApi } from '@/api/auth'
import { tokenStore } from '@/api/http'
import type { LoginPayload, RegisterPayload, User } from '@/types/domain'

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string | null>(tokenStore.get())
  const user = ref<User | null>(null)

  const isLoggedIn = computed(() => !!token.value)

  function setToken(next: string): void {
    token.value = next
    tokenStore.set(next)
  }

  function clear(): void {
    token.value = null
    user.value = null
    tokenStore.clear()
  }

  async function login(payload: LoginPayload): Promise<void> {
    const res = await loginApi(payload)
    setToken(res.access_token)
    user.value = await fetchMe()
  }

  async function register(payload: RegisterPayload): Promise<void> {
    const res = await registerApi(payload)
    // 注册接口直接返回 token，不需要再登录一次
    setToken(res.access_token)
    user.value = await fetchMe()
  }

  /**
   * 恢复会话。
   *
   * ## 为什么需要这一步
   *
   * token 存在 sessionStorage，刷新页面后 store 是空的。
   * 路由守卫只看「有没有 token」太粗 —— token 可能已过期。
   * 所以守卫放行后调用这个，用 `/auth/me` 验证一次。
   *
   * 失败时清空 token（请求层的 401 拦截也会触发，这里是兜底）。
   */
  async function restore(): Promise<boolean> {
    if (!token.value) return false
    if (user.value) return true
    try {
      user.value = await fetchMe()
      return true
    } catch {
      clear()
      return false
    }
  }

  return { token, user, isLoggedIn, login, register, restore, clear }
})
