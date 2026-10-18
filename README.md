# RAG 知识库问答系统

面向中文文档的检索增强问答。核心不是「接个大模型就能问」，而是两件事：

1. **解析质量可观测** —— 每份文档都有质量报告，答错时能判断是解析丢了内容还是检索没召回
2. **答案可追溯** —— 每句话带来源编号，点开能落到原文的页码 / 行号

> 产品架构图见 [`docs/diagrams/product-flow.svg`](docs/diagrams/product-flow.svg)

---

## 这是什么，不是什么

**是**：一个把文档变成可检索知识、并基于检索结果作答的系统。

**不是**：一个全知助手。它不猜、不补全、不在你的知识之外发言。库中没有就直说没有 —— 这是设计选择，不是能力缺陷。

**定位**：简历作品 / 学习项目。工程上认真做了取舍，但**不是生产级服务**，见下方「已知限制」。

---

## 启动顺序（顺序错了会踩坑）

```
1. 启动本地模型服务    ← LM Studio 加载 Qwen3-Embedding-4B，开启本地服务（端口 1234）
2. 启动基础设施        ← docker compose -f docker-compose.dev.yml up -d
3. 初始化数据库        ← make db-init
4. 启动后端            ← make dev
5. 启动前端            ← make web
```

**为什么顺序重要**：

- 模型服务没起就上传文档 → 索引任务会停在 `waiting_model`，不会失败但也不会完成
- 模型服务没起就提问 → 检索的第一步（问题向量化）直接失败
- 基础设施没起就启动后端 → 启动检查会报 `db_unreachable`

---

## 需要模型服务的时机

| 场景 | 需要 | 原因 |
|---|---|---|
| 上传文档后的索引 | ✅ | 切块后要向量化 |
| **每一次提问** | ✅ | 问题要先转成向量才能检索 |
| 写代码 / 跑单测 | ❌ | 解析、切块、引用校验都是纯函数；单测注入假 embedder |
| 启动服务 / 看质量看板 | ❌ | 启动不调模型 |

---

## 快速开始

```bash
cp .env.example .env      # 填 JWT_SECRET / FERNET_KEY / 数据库密码
make db-up                # 起 PG + Redis
make db-init              # 建表 + RLS + 应用角色
make dev                  # 起后端（Windows 下必须用这个，见下方说明）
```

> **快速开始文档建设中** —— 完整链路（上传 → 索引 → 提问）要等竖切片完成后才能跑通。
> 现在只能验证到「服务能起来、数据库能连」。

**Windows 用户注意**：不要用 `uvicorn app.main:app` 直接启动。
psycopg 异步模式不支持 Windows 默认的 ProactorEventLoop，
`make dev` 走的是 `scripts/dev.py`，它自己控制事件循环。

---

## 支持的文档格式

| 格式 | 状态 | 说明 |
|---|---|---|
| PDF | ✅ | 正文用 pypdf，表格用 pdfplumber |
| Word `.docx` | ✅ | 保留标题层级、表格、列表 |
| Excel `.xlsx` | ✅ | **合并单元格自动展开**，双行表头自动压平 |
| PPT `.pptx` | ✅ | **备注（notes）一并提取** |
| CSV / TSV | ✅ | 编码自动识别（GBK/UTF-8） |
| Markdown | ✅ | 标题层级、代码块、表格 |
| TXT / LOG | ✅ | 编码自动识别 |
| HTML | ✅ | 正文容器识别，剔除导航与脚本 |
| 扫描件 PDF | ❌ | **明确报错并提示不支持 OCR**，不会静默返回空内容 |
| `.doc/.xls/.ppt` | ❌ | 老格式，提示另存为新格式 |

**已知解析风险**（如实记录，不是待修 bug）：

| 风险 | 说明 |
|---|---|
| 双栏 PDF 阅读顺序 | pypdf 按物理流顺序输出，双栏可能左右串行。**素材库暂无真正的双栏样例，未验证** |
| 复杂表格 | 无边框表格、跨页表格可能识别失败，会产出 `table_detect_failed` 告警 |
| Excel 公式 | 若文件从未被 Excel 打开过，公式单元格没有缓存值，会产出 `formula_no_cache` 告警 |

---

## 文档索引

| 文档 | 内容 |
|---|---|
| [`docs/02-架构方案.md`](docs/02-架构方案.md) | **主文档**：13 项核心决策、数据模型、API 契约、开发排期 |
| [`docs/knowledge/技术说明.md`](docs/knowledge/技术说明.md) | 技术选型的解释性说明、RRF 原理与示例 |
| [`docs/diagrams/product-flow.svg`](docs/diagrams/product-flow.svg) | 产品架构图 |
| [`docs/diagrams/README.md`](docs/diagrams/README.md) | 架构图的栅格参数与改图规则 |
| [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) | 第三方依赖许可证 |

---

## 已知限制

**这些是明确的设计取舍，不是待修的 bug。**

| 限制 | 说明 |
|---|---|
| **无备份机制** | 用户数据只存一份。**请勿用于存放不可再生的重要文档** |
| **删除不可恢复** | 物理删除，但有「输入库名确认」防误操作 |
| **不支持 OCR** | 扫描件 PDF 会明确报错并提示，不会静默失败 |
| **不支持老格式** | `.doc/.xls/.ppt` 需另存为新格式 |
| **不支持网页抓取** | 只能上传本地文件，不接收 URL |
| **登录极简** | 6-12 位密码、无邮箱验证、无找回密码。**不建议用于生产** |
| **`query_logs` 只增不减** | 长期运行需自行归档 |
| **无多机扩展** | 单机部署，未做分片与水平扩展 |
| **一次查一个库** | 不做跨库联合检索 |

---

## 开发

```bash
make help          # 所有命令
make venv          # 创建虚拟环境并安装依赖
make test          # 跑单测（不需要模型服务）
make lint          # 代码检查
make db-reset      # 重置数据库（数据全丢）
make reprocess DOC=<id> STAGE=chunk   # 单文档单阶段重跑
make reindex       # 从 PG 全量重建索引
```

**排期与阶段验收标准**见架构文档 §13。

---

## 开源协议

MIT，见 [`LICENSE`](LICENSE)。
第三方依赖的许可证见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

本项目**不使用** AGPL 许可的组件（如 PyMuPDF），以免污染 MIT 分发物。
