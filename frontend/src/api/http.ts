/**
 * 请求层：所有 HTTP 请求都从这里出去。
 *
 * ## 为什么页面里不准直接写 fetch
 *
 * 401 跳登录、错误体解析、token 注入，这三件事如果散落在每个页面里，
 * 一定会有一处漏掉。收敛到这一层，改一次全都生效。
 *
 * ## 与后端错误体对齐
 *
 * 后端 `AppError.to_payload()` 返回：
 *
 *     { code, message, trace_id, detail? }
 *
 * - `message` 是**可以直接展示给用户**的中文
 * - `code` 留给前端做分支判断（如 `kb_not_ready`）
 * - `trace_id` 用于和后端日志对照
 *
 * 所以 `ApiError` 同时保留这两者，不要只留 message —— 否则遇到需要
 * 特殊处理的错误码，前端就只能去解析中文文案。
 */

/** 后端错误体（与 AppError.to_payload 一致） */
export interface ApiErrorBody {
  code: string
  message: string
  trace_id?: string
  detail?: Record<string, unknown>
}

/** 网络层失败（没拿到 HTTP 响应） */
export class NetworkError extends Error {
  constructor(message: string, cause?: unknown) {
    super(message)
    this.name = 'NetworkError'
    this.cause = cause
  }
}

/** 业务错误（拿到了 HTTP 响应，但状态码不是 2xx） */
export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly traceId?: string
  readonly detail?: Record<string, unknown>

  constructor(status: number, body: Partial<ApiErrorBody>) {
    // 兜底文案：后端理论上总会给 message，但代理层（如 502）可能返回 HTML
    super(body.message || `请求失败（HTTP ${status}）`)
    this.name = 'ApiError'
    this.status = status
    this.code = body.code || `http_${status}`
    this.traceId = body.trace_id
    this.detail = body.detail
  }
}

const TOKEN_KEY = 'kbqa.token'

/**
 * Token 读写。
 *
 * ## 为什么用 sessionStorage 而不是 localStorage
 *
 * 两者都能被 XSS 读取（这是前端存 token 的固有风险）。
 * 区别在生命周期：
 *   - localStorage：关掉浏览器还留着 —— 共用电脑时是隐患
 *   - sessionStorage：关掉标签页就失效 —— 演示场景更合适
 *
 * 代价是「刷新页面保留、关标签页要重登」，对演示不是问题。
 */
export const tokenStore = {
  get(): string | null {
    return sessionStorage.getItem(TOKEN_KEY)
  },
  set(token: string): void {
    sessionStorage.setItem(TOKEN_KEY, token)
  },
  clear(): void {
    sessionStorage.removeItem(TOKEN_KEY)
  },
}

/** 401 时的回调。由 router 注册，避免请求层直接依赖路由。 */
let onUnauthorized: (() => void) | null = null

export function setUnauthorizedHandler(handler: () => void): void {
  onUnauthorized = handler
}

/** 开发期用 `/api`（走 Vite 代理），生产可改成同域前缀 */
const BASE_URL = import.meta.env.VITE_API_BASE ?? '/api'

export interface RequestOptions extends Omit<RequestInit, 'body'> {
  /** JSON 请求体（会自动序列化 + 设 Content-Type） */
  json?: unknown
  /** 原始请求体（上传用 FormData 时走这里） */
  body?: BodyInit
  /** 超时毫秒数。上传需要单独放宽。 */
  timeoutMs?: number
  /** 是否跳过 401 处理（登录接口本身不该触发跳转） */
  skipAuthRedirect?: boolean
}

/**
 * 发一个请求并解析响应。
 *
 * 返回泛型 T 只是给调用方一个类型落点；真正的类型来自
 * `api/generated.d.ts`（由 openapi-typescript 生成）。
 */
export async function request<T>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const {
    json,
    body,
    timeoutMs = 30_000,
    skipAuthRedirect = false,
    headers,
    ...rest
  } = options

  const finalHeaders = new Headers(headers)

  const token = tokenStore.get()
  if (token) {
    finalHeaders.set('Authorization', `Bearer ${token}`)
  }

  let finalBody = body
  if (json !== undefined) {
    finalHeaders.set('Content-Type', 'application/json')
    finalBody = JSON.stringify(json)
  }

  // 超时用 AbortController 自己实现：不同浏览器的 fetch 对 timeout 支持不一致
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)

  let resp: Response
  try {
    resp = await fetch(`${BASE_URL}${path}`, {
      ...rest,
      headers: finalHeaders,
      body: finalBody,
      signal: options.signal ?? controller.signal,
    })
  } catch (err) {
    // abort 与网络错误要区分：前者是用户主动/超时，后者是真连不上
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new NetworkError('请求超时，请检查后端服务是否在运行', err)
    }
    throw new NetworkError('无法连接服务器，请检查网络或后端服务', err)
  } finally {
    clearTimeout(timer)
  }

  // 204 没有响应体
  if (resp.status === 204) {
    return undefined as T
  }

  const contentType = resp.headers.get('Content-Type') ?? ''
  const isJson = contentType.includes('application/json')

  if (!resp.ok) {
    // 401 统一跳登录（登录接口自己除外，否则登录失败会变成"跳登录"）
    if (resp.status === 401 && !skipAuthRedirect) {
      tokenStore.clear()
      onUnauthorized?.()
    }

    let payload: Partial<ApiErrorBody> = {}
    if (isJson) {
      try {
        payload = (await resp.json()) as ApiErrorBody
      } catch {
        // 后端返回了非法 JSON，退化为状态码错误
      }
    }
    throw new ApiError(resp.status, payload)
  }

  if (!isJson) {
    // 例如导出文件之类的非 JSON 响应，这里不做处理
    return (await resp.text()) as unknown as T
  }

  return (await resp.json()) as T
}
