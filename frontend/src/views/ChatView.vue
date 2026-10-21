<script setup lang="ts">
/**
 * 问答页（SSE 流式）。
 *
 * ## F1 状态：占位 + 流式自检
 *
 * F4 实现完整对话。这里先放一个「流式链路自检」按钮，
 * 用来验证架构里列为硬性验收的那件事：
 *
 *     SSE 经过 Vite 代理后，事件是**逐条到达**，不是最后一次性收到
 *
 * 为什么必须在 F1 验：流式一旦被缓冲，F4 的所有工作都是白做。
 *
 * ## 自检怎么用
 *
 * 需要一个有文档的知识库。F1 阶段通常还没有，所以这个自检会连不上 ——
 * 它验证的是「协议通不通」：能不能拿到事件、事件格式对不对。
 */
import { ref } from 'vue'
import { useRoute } from 'vue-router'

import { streamQuery } from '@/api/sse'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

const route = useRoute()
const kbId = String(route.params.id ?? '')

interface ProbeLine {
  at: number
  event: string
  text: string
}

const running = ref(false)
const lines = ref<ProbeLine[]>([])
const error = ref('')
let controller: AbortController | null = null

async function probe(): Promise<void> {
  if (running.value) return
  running.value = true
  lines.value = []
  error.value = ''
  controller = new AbortController()
  const started = performance.now()

  try {
    for await (const evt of streamQuery(
      kbId,
      '连通性自检：这份资料讲了什么？',
      [],
      { signal: controller.signal },
    )) {
      const at = Math.round(performance.now() - started)
      let text = ''
      if (evt.event === 'delta') text = evt.data.text
      else if (evt.event === 'error') text = evt.data.message
      else text = JSON.stringify(evt.data).slice(0, 160)

      lines.value.push({ at, event: evt.event, text })

      if (evt.event === 'end' || evt.event === 'error') break
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    running.value = false
    controller = null
  }
}

function abort(): void {
  controller?.abort()
}
</script>

<template>
  <div class="space-y-6">
    <div>
      <h1 class="text-xl font-semibold tracking-tight">问答</h1>
      <p class="mt-1 text-sm text-muted-foreground">
        kb_id = <code class="rounded bg-muted px-1 py-0.5 text-xs">{{ kbId }}</code>
        · F4 实现完整对话
      </p>
    </div>

    <Card>
      <CardHeader>
        <CardTitle class="text-base">SSE 流式自检</CardTitle>
        <CardDescription>
          验证事件逐条到达。若所有事件的时间戳几乎相同，说明流被代理缓冲了。
        </CardDescription>
      </CardHeader>
      <CardContent class="space-y-3">
        <div class="flex gap-2">
          <Button size="sm" :disabled="running" @click="probe">开始自检</Button>
          <Button v-if="running" size="sm" variant="outline" @click="abort">中断</Button>
        </div>

        <p v-if="error" class="rounded-md bg-status-failed-bg px-3 py-2 text-sm text-status-failed">
          {{ error }}
        </p>

        <div v-if="lines.length" class="max-h-72 space-y-1 overflow-auto rounded-md border p-3">
          <div
            v-for="(l, i) in lines"
            :key="i"
            class="flex gap-3 font-mono text-xs"
          >
            <span class="w-14 shrink-0 text-muted-foreground">+{{ l.at }}ms</span>
            <span class="w-12 shrink-0 text-status-busy">{{ l.event }}</span>
            <span class="break-all">{{ l.text }}</span>
          </div>
        </div>
      </CardContent>
    </Card>
  </div>
</template>
