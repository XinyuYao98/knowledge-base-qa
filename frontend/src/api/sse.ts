/**
 * SSE 流式解析。
 *
 * ## 为什么不用 EventSource
 *
 * 后端 `/query/stream` 要 `Authorization: Bearer xxx` 头。
 * `EventSource` **不支持自定义请求头**，只能把 token 塞进 URL：
 *
 *     new EventSource('/api/query/stream?token=xxx')
 *
 * 这会让凭证出现在浏览器历史、代理日志、Nginx 的 access log 里。
 * 所以用 `fetch` + `ReadableStream` 手动解析。
 *
 * ## 后端的实际协议（来自 `app/api/routes/query.py` 的 `_sse`）
 *
 *     event: meta
 *     data: {"citations": [...], "question": "...", "rewritten": false, "retrieve_ms": 812.3}
 *
 *     event: delta
 *     data: {"text": "一线"}
 *
 *     event: done
 *     data: {"answer": "...", "citation_ok": true, "caveat": "", ...}
 *
 *     event: end
 *     data: {}
 *
 * 注意有 **`end` 和 `done` 两个收尾事件**：`done` 带最终结果，`end` 只是关闭标记。
 * 漏掉 `done` 就拿不到引用和 `citation_ok`。
 */

/** 引用来源（与后端 `_citation_payload` 对齐） */
export interface Citation {
  index: number
  filename: string
  page?: number | null
  sheet?: string | null
  heading_path?: string | null
  snippet?: string
  chunk_id?: string
}

export interface StreamMeta {
  citations: Citation[]
  question: string
  rewritten: boolean
  retrieve_ms: number
}

export interface StreamDone {
  answer: string
  citations: Citation[]
  citation_ok: boolean
  caveat: string
  duration_ms: number
  degraded?: string[]
  model?: string
}

export type StreamEvent =
  | { event: 'meta'; data: StreamMeta }
  | { event: 'delta'; data: { text: string } }
  | { event: 'done'; data: StreamDone }
  | { event: 'error'; data: { message: string } }
  | { event: 'end'; data: Record<string, never> }

export interface StreamOptions {
  signal?: AbortSignal
}

/**
 * 把 fetch 的字节流解析成 SSE 事件。
 *
 * ## 三个必须处理的边界（都实测过）
 *
 * **1. 分帧不完整**
 *    一个 TCP chunk 可能切在 `data: {"te` 中间。
 *    必须按 `\n\n` 缓冲，凑齐一个完整事件再解析。
 *
 * **2. 多字节字符跨 chunk 被截断**
 *    中文 UTF-8 是 3 字节。`TextDecoder.decode()` 默认把不完整的尾部丢掉，
 *    必须用 `{ stream: true }` 让它把残字节留到下次拼。
 *
 * **3. 空行分隔与 `event:` / `data:` 多行**
 *    SSE 规范里一个 `data:` 可能重复出现（多行拼成一个 data）。
 *    后端目前每事件单行 data，但解析器要按规范写，否则将来后端改格式就崩。
 */
async function* parseSSE(
  body: ReadableStream<Uint8Array>,
  signal?: AbortSignal,
): AsyncGenerator<StreamEvent> {
  const reader = body.getReader()
  // stream: true —— 保留被截断的多字节字符，等下一块补齐
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  try {
    while (true) {
      if (signal?.aborted) break
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })

      // 事件之间用空行分隔；兼容 \r\n
      let sep = buffer.indexOf('\n\n')
      while (sep !== -1) {
        const raw = buffer.slice(0, sep)
        buffer = buffer.slice(sep + 2)

        const parsed = parseBlock(raw)
        if (parsed) yield parsed

        sep = buffer.indexOf('\n\n')
      }
    }

    // 收尾：刷新 decoder 里可能残留的字节
    buffer += decoder.decode()
    if (buffer.trim()) {
      const parsed = parseBlock(buffer)
      if (parsed) yield parsed
    }
  } finally {
    reader.releaseLock()
  }
}

/** 解析单个 SSE 块（`event: x\ndata: {...}`） */
function parseBlock(raw: string): StreamEvent | null {
  const lines = raw.split('\n')
  let event = 'message'
  const dataLines: string[] = []

  for (const line of lines) {
    if (line.startsWith('event:')) {
      event = line.slice(6).trim()
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice(5).trimStart())
    }
    // 注释行（以 `:` 开头）和未知字段直接忽略
  }

  if (!dataLines.length) return null

  const dataText = dataLines.join('\n')
  let data: unknown
  try {
    data = JSON.parse(dataText)
  } catch {
    // 后端理论上总是发 JSON；解析失败说明协议被改了，明确报出来
    return {
      event: 'error',
      data: { message: `无法解析的服务端事件：${dataText.slice(0, 80)}` },
    }
  }

  return { event, data } as StreamEvent
}

/**
 * 发起流式问答，逐个 yield 事件。
 *
 * 用 AsyncGenerator 而不是回调：调用方在 Vue 里 `for await` 直接改
 * 响应式变量，比回调层层传递更直观。
 *
 * 用法：
 *
 *     for await (const evt of streamQuery(kbId, question, history, { signal })) {
 *       if (evt.event === 'delta') answer += evt.data.text
 *     }
 */
export async function* streamQuery(
  kbId: string,
  question: string,
  history: Array<{ role: string; content: string }>,
  options: StreamOptions = {},
): AsyncGenerator<StreamEvent> {
  // 动态 import 避免循环依赖：http.ts 不认识本文件，本文件需要 token 与 BASE_URL
  const { tokenStore } = await import('./http')
  const BASE_URL = import.meta.env.VITE_API_BASE ?? '/api'

  const token = tokenStore.get()
  const resp = await fetch(`${BASE_URL}/kbs/${kbId}/query/stream`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ question, history }),
    signal: options.signal,
  })

  if (!resp.ok) {
    // 流式接口的非 2xx 一般也是 JSON 错误体，交给调用方统一展示
    let message = `请求失败（HTTP ${resp.status}）`
    try {
      const body = (await resp.json()) as { message?: string }
      if (body.message) message = body.message
    } catch {
      // 忽略：保持默认文案
    }
    yield { event: 'error', data: { message } }
    return
  }

  if (!resp.body) {
    yield { event: 'error', data: { message: '服务端没有返回流式响应体' } }
    return
  }

  yield* parseSSE(resp.body, options.signal)
}
