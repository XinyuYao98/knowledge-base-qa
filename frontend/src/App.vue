<script setup lang="ts">
/**
 * 应用根组件。
 *
 * F1 只负责挂载布局与路由出口。登录页不需要顶栏，
 * 所以由 `AppLayout` 内部按路由判断是否渲染导航。
 */
import AppLayout from '@/components/AppLayout.vue'
</script>

<template>
  <AppLayout>
    <RouterView />
  </AppLayout>
</template>
