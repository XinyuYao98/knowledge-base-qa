import path from 'node:path'

import tailwindcss from '@tailwindcss/vite'
import vue from '@vitejs/plugin-vue'
import { defineConfig, loadEnv } from 'vite'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')

  // 后端地址做成可配置：换端口或换机器时不用改代码。
  // 在 `.env.development` 里写 VITE_PROXY_TARGET=http://127.0.0.1:8090 即可。
  const target = env.VITE_PROXY_TARGET || 'http://127.0.0.1:8080'

  return {
    plugins: [vue(), tailwindcss()],

    resolve: {
      alias: {
        // `@/` 指向 src。shadcn-vue 组件之间也按这个约定互相引用。
        '@': path.resolve(import.meta.dirname, './src'),
      },
    },

    server: {
      port: 5173,
      // ## 为什么走代理而不是让前端直连后端
      //
      // 1. 前端代码里只写 `/api/xxx`，将来改部署形态不用动代码
      // 2. 同源请求，少一层 CORS 变量
      //
      // `rewrite` 不能省：后端路由本身没有 `/api` 前缀。
      proxy: {
        '/api': {
          target,
          changeOrigin: true,
          rewrite: (p) => p.replace(/^\/api/, ''),
        },
      },
    },
  }
})
