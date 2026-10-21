<script setup lang="ts">
/**
 * 登录 / 注册页。
 *
 * ## F1 状态：占位
 *
 * F2 实现。要点（来自架构 §7.1）：
 *   - 用户名 + 密码（**不是邮箱**，后端字段是 `username`）
 *   - 用户名 3–20（DB 约束比 Pydantic 的 32 更严）
 *   - 密码 ≥ 8
 *   - 成功后跳 `redirect` 参数指定的路径
 *
 * 依赖：`api/auth.ts` 的 `login` / `register`，`stores/auth.ts`
 */
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
</script>

<template>
  <div class="flex min-h-screen items-center justify-center px-6">
    <Card class="w-full max-w-sm">
      <CardHeader>
        <CardTitle class="text-lg">知识库问答</CardTitle>
        <CardDescription>F2 将在此实现登录与注册。</CardDescription>
      </CardHeader>
      <CardContent class="space-y-3">
        <p class="text-sm text-muted-foreground">
          当前是 F1 骨架，认证接口（<code class="rounded bg-muted px-1 py-0.5 text-xs">/auth/*</code>）
          尚未接入。
        </p>
        <Button class="w-full" disabled>登录（F2 实现）</Button>
      </CardContent>
    </Card>
  </div>
</template>
