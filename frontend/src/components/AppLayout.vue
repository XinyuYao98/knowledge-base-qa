<script setup lang="ts">
/**
 * 布局壳：顶栏 + 内容区。
 *
 * ## 为什么登录页不显示顶栏
 *
 * 登录页是「未进入系统」的状态，给它挂一个带用户名和退出按钮的顶栏
 * 会显得结构混乱。这里按路由 meta 判断。
 *
 * ## F1 的顶栏只放产品名
 *
 * 导航项（知识库 / 问答）在 F2 有了知识库上下文后再加 ——
 * 现在加会得到一个「点了没反应」的空导航。
 */
import { computed } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()
const showChrome = computed(() => !route.meta.public)
</script>

<template>
  <div class="min-h-screen bg-background text-foreground">
    <header
      v-if="showChrome"
      class="sticky top-0 z-20 border-b bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/75"
    >
      <div class="mx-auto flex h-14 max-w-6xl items-center gap-3 px-6">
        <RouterLink to="/kbs" class="flex items-center gap-2">
          <span class="size-2 rounded-full bg-primary" aria-hidden="true" />
          <span class="text-sm font-semibold tracking-tight">知识库问答</span>
        </RouterLink>

        <nav class="ml-4 flex items-center gap-1 text-sm text-muted-foreground">
          <RouterLink
            to="/kbs"
            class="rounded-md px-2.5 py-1 transition-colors hover:bg-accent hover:text-accent-foreground"
            active-class="bg-accent text-accent-foreground"
          >
            知识库
          </RouterLink>
        </nav>
      </div>
    </header>

    <main :class="showChrome ? 'mx-auto max-w-6xl px-6 py-8' : ''">
      <slot />
    </main>
  </div>
</template>
