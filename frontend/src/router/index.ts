/**
 * 路由表。
 *
 * F1 只装配骨架，页面用占位组件，F2–F4 逐个替换。
 * 占位页不是「空文件」——每个都写明「这一页要做什么、依赖哪几个接口」，
 * 这样 F2 开工时不需要回头看架构文档。
 */

import { createRouter, createWebHistory } from 'vue-router'

import { useAuthStore } from '@/stores/auth'
import { setUnauthorizedHandler } from '@/api/http'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/LoginView.vue'),
      meta: { public: true, title: '登录' },
    },
    {
      path: '/',
      redirect: '/kbs',
    },
    {
      path: '/kbs',
      name: 'kb-list',
      component: () => import('@/views/KbListView.vue'),
      meta: { title: '知识库' },
    },
    {
      path: '/kbs/:id',
      name: 'kb-detail',
      component: () => import('@/views/KbDetailView.vue'),
      meta: { title: '知识库详情' },
    },
    {
      path: '/kbs/:id/chat',
      name: 'chat',
      component: () => import('@/views/ChatView.vue'),
      meta: { title: '问答' },
    },
    {
      // 未匹配的路径统一回列表，避免演示时出现白屏
      path: '/:pathMatch(.*)*',
      redirect: '/kbs',
    },
  ],
})

/**
 * 登录守卫。
 *
 * ## 只做粗判，不做 token 有效性校验
 *
 * 这里只检查「有没有 token」。校验有效性要发请求，放进守卫会让每次
 * 切路由都多一次往返。真正的过期信号是第一个接口返回 401，
 * 由请求层的拦截器处理（见 `setUnauthorizedHandler`）。
 */
router.beforeEach(async (to) => {
  const auth = useAuthStore()

  if (to.meta.public) {
    // 已登录还去登录页 → 回列表
    if (auth.isLoggedIn && (await auth.restore())) {
      return { name: 'kb-list' }
    }
    return true
  }

  if (!auth.isLoggedIn) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }

  // 刷新后 store 为空，用 /auth/me 恢复一次
  const ok = await auth.restore()
  if (!ok) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }

  return true
})

router.afterEach((to) => {
  const title = (to.meta.title as string | undefined) ?? '知识库问答'
  document.title = `${title} · 知识库问答`
})

/**
 * 注册 401 处理器。
 *
 * 请求层不直接依赖 router（否则 http.ts 要 import 整个路由表，
 * 单测和循环依赖都会变麻烦），所以用回调注入。
 */
setUnauthorizedHandler(() => {
  const current = router.currentRoute.value
  if (current.name === 'login') return
  router.replace({ name: 'login', query: { redirect: current.fullPath } })
})

export default router
