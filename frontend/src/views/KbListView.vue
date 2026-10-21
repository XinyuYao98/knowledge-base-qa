<script setup lang="ts">
/**
 * 知识库列表。
 *
 * ## F1 状态：占位 + 连通性自检
 *
 * F2 实现列表/新建/删除。这里先放一个「后端连通性」卡片，
 * 用来验证 F1 的三件事是否成立：
 *   1. Vite 代理 `/api` 能打到 8080
 *   2. CORS 配置正确
 *   3. 请求层的错误处理能给出可读提示
 */
import { onMounted, ref } from 'vue'

import { request, ApiError, NetworkError } from '@/api/http'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'

type HealthState = 'idle' | 'loading' | 'ok' | 'fail'

const state = ref<HealthState>('idle')
const detail = ref('')

async function checkHealth(): Promise<void> {
  state.value = 'loading'
  detail.value = ''
  try {
    // /health 不需要鉴权，是最干净的连通性探针
    const res = await request<{ status?: string }>('/health')
    state.value = 'ok'
    detail.value = JSON.stringify(res)
  } catch (err) {
    state.value = 'fail'
    if (err instanceof ApiError) {
      detail.value = `${err.status} · ${err.message}`
    } else if (err instanceof NetworkError) {
      detail.value = err.message
    } else {
      detail.value = String(err)
    }
  }
}

onMounted(checkHealth)
</script>

<template>
  <div class="space-y-6">
    <div class="flex items-end justify-between">
      <div>
        <h1 class="text-xl font-semibold tracking-tight">知识库</h1>
        <p class="mt-1 text-sm text-muted-foreground">
          F2 将在此实现库列表、新建与删除。
        </p>
      </div>
    </div>

    <Card>
      <CardHeader>
        <div class="flex items-center gap-2">
          <CardTitle class="text-base">后端连通性自检</CardTitle>
          <Badge v-if="state === 'ok'" class="bg-status-ready-bg text-status-ready">
            正常
          </Badge>
          <Badge v-else-if="state === 'fail'" class="bg-status-failed-bg text-status-failed">
            失败
          </Badge>
          <Badge v-else variant="secondary">{{ state === 'loading' ? '检测中' : '未检测' }}</Badge>
        </div>
        <CardDescription>
          验证 Vite 代理 <code class="rounded bg-muted px-1 py-0.5 text-xs">/api</code>
          能打到后端 8080。
        </CardDescription>
      </CardHeader>
      <CardContent class="space-y-3">
        <p v-if="detail" class="break-all rounded-md bg-muted px-3 py-2 font-mono text-xs text-muted-foreground">
          {{ detail }}
        </p>
        <Button variant="outline" size="sm" :disabled="state === 'loading'" @click="checkHealth">
          重新检测
        </Button>
      </CardContent>
    </Card>
  </div>
</template>
