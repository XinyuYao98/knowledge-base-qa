#!/usr/bin/env node
/**
 * shadcn-vue 组件安装器（curl 版）。
 *
 * ## 为什么不用官方 CLI
 *
 * `npx shadcn-vue init` 在本机报：
 *
 *     Failed to fetch from registry: https://shadcn-vue.com/r/styles/index.json
 *
 * 但同一个 URL，`curl` 和 Node 原生 `fetch` 都能拿到 200（实测 590ms）。
 * 说明是 CLI 内部的 HTTP 客户端有问题（它可能带了被 CDN 拒绝的头部），
 * 不是网络问题。
 *
 * shadcn 的本质就是「把组件源码复制进项目」，注册表只是一个 JSON。
 * 所以这里用 `child_process` 调 curl 下载，然后按注册表里的 `files` 落盘。
 *
 * ## 用法
 *
 *     node scripts/install-shadcn.mjs button card input
 *
 * 依赖会自动解析（包括 registryDependencies 里的组件）。
 */

import { execFileSync } from 'node:child_process'
import { mkdirSync, writeFileSync, existsSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'

const STYLE = 'new-york'
const BASE = `https://shadcn-vue.com/r/styles/${STYLE}`

const ROOT = resolve(import.meta.dirname, '..')
const UI_DIR = join(ROOT, 'src', 'components', 'ui')

/** 已经装过的组件，避免重复下载 */
const installed = new Set()

/**
 * 用 curl 拉取 JSON。
 *
 * 用 execFileSync 而不是 fetch：见文件头说明。
 * `-f` 让 curl 在 4xx/5xx 时返回非零退出码，避免把错误页当 JSON 解析。
 */
function fetchJson(url) {
  const out = execFileSync('curl', ['-sf', '--max-time', '30', url], {
    encoding: 'utf-8',
  })
  return JSON.parse(out)
}

/** 组件名 → 文件路径。路径来自注册表的 `files[].path`。 */
function installComponent(name) {
  if (installed.has(name)) return
  installed.add(name)

  process.stdout.write(`  · ${name} `)

  let meta
  try {
    meta = fetchJson(`${BASE}/${name}.json`)
  } catch {
    console.log('✗ 注册表里没有这个组件')
    process.exitCode = 1
    return
  }

  // 先装依赖的组件（如 dialog 依赖 button）
  for (const dep of meta.registryDependencies ?? []) {
    // registryDependencies 可能是完整 URL，也可能是组件名
    const depName = dep.includes('/') ? dep.split('/').pop().replace('.json', '') : dep
    if (!installed.has(depName)) installComponent(depName)
  }

  let written = 0
  for (const file of meta.files ?? []) {
    // 注册表里的 path 形如 "ui/button/Button.vue"，
    // 需要落到 src/components/ui/button/Button.vue
    const rel = file.path.replace(/^ui\//, '')
    const dest = join(UI_DIR, rel)
    mkdirSync(dirname(dest), { recursive: true })

    // ## 必须重写组件内部的 import 路径
    //
    // 注册表里的源码写的是 `@/registry/new-york/ui/button`（CLI 自己运行时会改写），
    // 我们直接用 curl 落盘就绕过了这一步，导致构建报：
    //     Cannot find module '@/registry/new-york/ui/button'
    // 实测踩到（alert-dialog 的两个文件）。
    const content = file.content.replace(
      /@\/registry\/[a-z-]+\/ui\//g,
      '@/components/ui/',
    )
    writeFileSync(dest, content, 'utf-8')
    written += 1
  }

  const deps = meta.dependencies?.length ? ` (npm: ${meta.dependencies.join(', ')})` : ''
  console.log(`✓ ${written} 个文件${deps}`)
}

const names = process.argv.slice(2)
if (!names.length) {
  console.error('用法: node scripts/install-shadcn.mjs <组件名...>')
  process.exit(1)
}

if (!existsSync(UI_DIR)) {
  mkdirSync(UI_DIR, { recursive: true })
}

console.log(`安装 shadcn-vue 组件（style=${STYLE}）`)
for (const name of names) {
  installComponent(name)
}
console.log('完成')
