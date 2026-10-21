#!/usr/bin/env node
/**
 * 从后端拉 OpenAPI 并生成 TypeScript 类型。
 *
 * ## 为什么要脚本化而不是每次手敲 curl + npx
 *
 * 「后端改了字段，前端类型没跟上」是这个项目已经踩过的坑
 * （P6 三次凭印象写接口，被真实字段纠正）。
 * 把两步合成一条命令，并且写进 README，才会真的被执行。
 *
 * ## 用法
 *
 *     npm run gen:api
 *
 * 后端地址取 `VITE_PROXY_TARGET`，默认 http://127.0.0.1:8080。
 */

import { execFileSync } from 'node:child_process'
import { readFileSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'

const ROOT = resolve(import.meta.dirname, '..')

/** 读 .env.development 里的 VITE_PROXY_TARGET（不引入 dotenv 依赖） */
function readTarget() {
  if (process.env.VITE_PROXY_TARGET) return process.env.VITE_PROXY_TARGET
  try {
    const env = readFileSync(join(ROOT, '.env.development'), 'utf-8')
    const m = env.match(/^VITE_PROXY_TARGET=(.+)$/m)
    if (m) return m[1].trim()
  } catch {
    // 没有 .env.development 时用默认值
  }
  return 'http://127.0.0.1:8080'
}

const target = readTarget()
const url = `${target.replace(/\/$/, '')}/openapi.json`
const outFile = join(ROOT, 'openapi.json')

console.log(`拉取 ${url}`)

let json
try {
  json = execFileSync('curl', ['-sf', '--max-time', '20', url], { encoding: 'utf-8' })
} catch {
  console.error(
    `\n✗ 拉取失败。请确认后端在运行（make dev）。\n  当前目标：${url}`,
  )
  process.exit(1)
}

// 格式化后写入，便于 git diff 看清楚接口变了什么
writeFileSync(outFile, JSON.stringify(JSON.parse(json), null, 2) + '\n', 'utf-8')
console.log(`✓ 已写入 openapi.json（${json.length} 字节）`)

console.log('生成 TypeScript 类型…')
execFileSync(
  'npx',
  ['openapi-typescript', 'openapi.json', '-o', 'src/api/generated.d.ts'],
  { cwd: ROOT, stdio: 'inherit', shell: true },
)

console.log('✓ 完成：src/api/generated.d.ts')
