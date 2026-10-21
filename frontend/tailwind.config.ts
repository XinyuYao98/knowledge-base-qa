import type { Config } from 'tailwindcss'

/**
 * Tailwind 配置。
 *
 * ## 为什么 Tailwind 4 还需要这个文件
 *
 * Tailwind 4 本身把配置移到了 CSS 里（`@theme` 块），原则上不需要 JS 配置。
 * 但 **shadcn-vue 的 CLI 在初始化时会检查这个文件是否存在**，
 * 不存在就拒绝继续（实测报 “No Tailwind CSS configuration found”）。
 *
 * 所以这里保留一个最小配置：内容检测 + 别名扩展。
 * 真正的主题变量在 `src/styles/index.css` 的 `@theme` 里。
 */
export default {
  content: ['./index.html', './src/**/*.{vue,js,ts,jsx,tsx}'],
} satisfies Config
