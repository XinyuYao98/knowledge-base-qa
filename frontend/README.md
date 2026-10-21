# 前端（kbqa-web）

知识库问答系统的 Web 界面。技术栈：**Vue 3 + Vite + Tailwind 4 + shadcn-vue**。

架构文档：[`../docs/07-前端技术架构.md`](../docs/07-前端技术架构.md)

---

## 启动

前提：后端已在运行（项目根目录 `make dev`）。

```bash
cd frontend
npm install          # 首次
npm run dev          # → http://localhost:5173
```

前端通过 Vite 代理访问后端，代码里只写 `/api/xxx`。

### 后端端口不是 8080

本机 8080 常被占用。改 `frontend/.env.development`：

```bash
VITE_PROXY_TARGET=http://127.0.0.1:8090
```

不用改代码。

---

## 常用命令

| 命令 | 作用 |
|---|---|
| `npm run dev` | 开发服务器（5173） |
| `npm run build` | 类型检查 + 生产构建 |
| `npm run lint` | ESLint |
| `npm run gen:api` | 从后端拉 OpenAPI 并生成类型 |
| `npm run shadcn:add <组件名>` | 装 shadcn-vue 组件 |

---

## 两处必须知道的约定

### 1. 接口类型要重新生成

后端改了接口，**必须**重跑：

```bash
npm run gen:api
```

它会更新 `openapi.json` 和 `src/api/generated.d.ts`，**两个文件都要提交**。

为什么手工维护不够：这个项目在 P6 三次凭印象写接口（`CHAT_MODEL`、`CitationCheck`、
`query_logs` 字段），每次都被真实接口纠正。生成类型能在编译期就暴露不一致。

### 2. 页面里不准直接写 `fetch`

所有请求走 `src/api/http.ts`，它负责：

- 注入 `Authorization: Bearer` 头
- 401 时清 token 并跳登录
- 把后端的 `{code, message, trace_id}` 转成 `ApiError`

散落在页面里写 `fetch`，这三件事一定有一处会漏。

---

## shadcn-vue 组件怎么装

官方 CLI 在本机跑不通（报 `Failed to fetch from registry`，
但同一个 URL 用 curl 和 Node fetch 都能拿到 200 —— 是 CLI 内部的 HTTP 客户端问题）。

所以用脚本代替：

```bash
npm run shadcn:add button card dialog
```

脚本用 `curl` 拉注册表中的组件源码，同时会：

- 自动装依赖组件（`registryDependencies`）
- **把组件内部的 `@/registry/xxx/ui/` 重写成 `@/components/ui/`**
  （不重写会构建失败，实测踩到）

生成的文件在 `src/components/ui/`，**可以直接改**——这就是 shadcn 的设计。

---

## 目录

```
src/
├── api/
│   ├── http.ts          请求层：Bearer / 401 / 错误映射
│   ├── sse.ts           SSE 解析（fetch + ReadableStream）
│   ├── auth.ts          登录 / 注册 / me
│   ├── kbs.ts           知识库 / 文档列表
│   └── generated.d.ts   openapi-typescript 生成，勿手改
├── stores/              Pinia：只有 auth
├── router/              路由 + 登录守卫
├── components/
│   ├── AppLayout.vue    顶栏 + 内容区
│   └── ui/              shadcn-vue 组件
├── views/               四个页面
├── types/domain.ts      语义化类型（字段已对照后端核对）
├── lib/utils.ts         cn()：Tailwind 类名合并
└── assets/index.css     shadcn 主题变量
```

---

## 已知限制

- **不做单元测试**（架构 §十二 的主动取舍），靠每期验收 checklist 把关
- 只支持浅色主题（变量已按 shadcn 约定写好，加 `.dark` 即可扩展）
- 无移动端适配
