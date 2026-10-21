import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

/**
 * 合并 Tailwind 类名。
 *
 * ## 为什么需要这个函数，而不是直接拼字符串
 *
 * Tailwind 的类之间有**优先级冲突**：
 *
 *     cn('px-4', 'px-2')   // 直接拼字符串得到 'px-4 px-2'，最终生效哪个取决于 CSS 顺序
 *     cn('px-4', 'px-2')   // 用 twMerge 得到 'px-2'，后者稳定覆盖前者
 *
 * shadcn 的组件全部接受 `class` prop 来做样式覆盖，
 * 没有 twMerge 的话「传进去的自定义样式被组件默认样式盖掉」会成为常态。
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs))
}
