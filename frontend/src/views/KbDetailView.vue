<script setup lang="ts">
/**
 * 知识库详情（文档列表 + 上传）。
 *
 * ## F1 状态：占位
 *
 * F3 实现。要点（来自架构 §7.3 / §7.4）：
 *   - 文档表格：文件名 / 类型 / 大小 / 状态 / 时间
 *   - 状态徽标：处理中（蓝）/ 可用（绿）/ 失败（红 + Tooltip）
 *   - 有中间态时 3s 轮询 `listDocuments`
 *   - 上传用 `XMLHttpRequest`（fetch 拿不到上传进度）
 *
 * ## 后端缺口（F3 开工前需要补）
 *
 * `documents.py` 只有 `POST ""`（上传）。
 * 「删除文档」「重建索引」在架构 §7.3 里写了，但后端没有端点。
 */
import { useRoute } from 'vue-router'

import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

const route = useRoute()
</script>

<template>
  <div class="space-y-6">
    <div>
      <h1 class="text-xl font-semibold tracking-tight">知识库详情</h1>
      <p class="mt-1 text-sm text-muted-foreground">
        kb_id = <code class="rounded bg-muted px-1 py-0.5 text-xs">{{ route.params.id }}</code>
      </p>
    </div>

    <Card>
      <CardHeader>
        <div class="flex items-center gap-2">
          <CardTitle class="text-base">文档与索引</CardTitle>
          <Badge variant="secondary">F3 实现</Badge>
        </div>
        <CardDescription>
          文件列表、索引进度轮询、拖拽上传。
        </CardDescription>
      </CardHeader>
      <CardContent>
        <p class="text-sm text-muted-foreground">
          当前没有文档。
        </p>
      </CardContent>
    </Card>
  </div>
</template>
