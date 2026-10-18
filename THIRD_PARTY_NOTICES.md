# 第三方依赖声明

本项目以 **MIT License** 发布（见 `LICENSE`）。以下第三方组件随本仓库分发或作为运行时依赖使用，
各自遵循其原始许可证。本项目**未修改**任何第三方组件的源代码。

生成时间：2026-10-17
适用版本：见 `backend/pyproject.toml`

---

## 一、运行时依赖（Python）

### MIT / BSD / Apache-2.0（与本项目协议兼容）

| 组件 | 版本 | 许可证 | 用途 |
|---|---|---|---|
| FastAPI | 0.143.3 | MIT | Web 框架 |
| uvicorn | 0.54.0 | BSD-3-Clause | ASGI 服务器 |
| pydantic | 2.14.1 | MIT | 数据校验 |
| pydantic-settings | 2.16.0 | MIT | 配置管理 |
| SQLAlchemy | 2.1.5 | MIT | ORM |
| alembic | 1.20.0 | MIT | 数据库迁移 |
| redis-py | 8.1.0 | MIT | Redis 客户端 |
| httpx | 0.28.1 | BSD-3-Clause | HTTP 客户端 |
| tenacity | 9.2.1 | Apache-2.0 | 重试 |
| structlog | 26.1.0 | Apache-2.0 / MIT | 结构化日志 |
| tiktoken | 0.14.0 | MIT | Token 计数 |
| PyJWT | 2.15.1 | MIT | JWT |
| argon2-cffi | 25.1.0 | MIT | 密码哈希 |
| cryptography | 50.0.2 | Apache-2.0 / BSD-3-Clause | Fernet 加密 |
| python-multipart | 0.0.32 | Apache-2.0 | 表单解析 |
| **chromadb** | **1.5.9** | **Apache-2.0** | 向量库客户端 |
| **langchain-core** | **1.6.12** | **MIT** | LLM 应用框架（仅用组件层） |
| **langchain-openai** | **1.7.2** | **MIT** | OpenAI 兼容客户端 |
| **langchain-chroma** | **1.1.0** | **MIT** | Chroma vectorstore 封装 |
| **jieba** | **0.42.1** | **MIT** | 中文分词 |
| **rank-bm25** | **0.2.2** | **Apache-2.0** | BM25 检索 |

### 文档解析

| 组件 | 版本 | 许可证 | 用途 |
|---|---|---|---|
| pypdf | 6.21.0 | BSD-3-Clause | PDF 文本抽取 |
| pdfplumber | 0.11.10 | MIT | PDF 表格抽取 |
| python-docx | 1.2.0 | MIT | DOCX |
| python-pptx | 1.0.2 | MIT | PPTX |
| openpyxl | 3.1.5 | MIT | XLSX |
| markdown-it-py | 4.2.0 | MIT | Markdown |
| beautifulsoup4 | 4.15.0 | MIT | HTML |
| lxml | 6.1.3 | BSD-3-Clause | HTML/XML 解析 |
| charset-normalizer | 3.5.2 | MIT | 编码探测 |

### LGPL（使用时需注意）

| 组件 | 版本 | 许可证 | 说明 |
|---|---|---|---|
| psycopg | 3.3.6 | **LGPL-3.0-only** | PostgreSQL 驱动 |

> **LGPL-3.0 说明**：本项目通过 `import psycopg` 以动态链接方式使用该库，**未修改其源代码**，
> 因此不触发 LGPL 的源码开放义务（LGPL 第 4 条动态链接豁免）。
> 若你修改了 psycopg 源码并分发，则需按 LGPL-3.0 公开修改部分。

### 开发依赖（不随产品分发）

| 组件 | 版本 | 许可证 |
|---|---|---|
| pytest | 9.1.1 | MIT |
| pytest-asyncio | 1.4.0 | Apache-2.0 |
| ruff | 0.14.4 | MIT |

---

## 二、运行时依赖（前端）

| 组件 | 许可证 |
|---|---|
| Next.js 15 | MIT |
| React 19 | MIT |
| Tailwind CSS | MIT |

---

## 三、外部服务与模型

**本项目不捆绑、不分发以下内容**，仅在运行时由用户自行部署或配置：

| 项目 | 许可证 | 说明 |
|---|---|---|
| **Qwen3-Embedding-4B** | **Apache-2.0** | 向量模型。由用户自行下载 GGUF 权重并在本地运行 |
| **Qwen3-Reranker-0.6B** | **Apache-2.0** | 重排模型。同上 |
| **LM Studio** | 专有（免费使用） | 本地模型服务宿主。由用户自行安装 |
| **llama-cpp-python** | MIT | 可选的本地方案 |
| PostgreSQL 16 | PostgreSQL License | 基础数据库 |
| ChromaDB Server | Apache-2.0 | 向量库服务 |
| Redis 7 | BSD-3-Clause | 缓存 |

> **如果你把模型权重打进 Docker 镜像分发**，需同时附带该模型的 Apache-2.0 许可证与
> `NOTICE` 文件（Qwen 官方仓库提供）。本项目的默认方案是：**权重在首次启动时自动下载**，
> 不随镜像分发，以避免许可证分发义务与镜像体积膨胀。

---

## 四、明确未使用的组件（供对照）

| 组件 | 未使用原因 |
|---|---|
| **PyMuPDF** | AGPL-3.0，会污染 MIT 分发物。改用 pypdf + pdfplumber（双 MIT） |
| **pgvector** | 本项目已改用 ChromaDB 存向量 |
| **Elasticsearch** | 增加一个重型组件，收益仅为"更标准的 BM25" |
| **OpenAI API** | 问答模型由用户自带，本项目不代理、不转售 |

---

## 五、许可证合规提示

1. 本项目为 MIT，**允许商业使用、修改、闭源分发**，但需保留版权声明与许可证全文
2. 若你分发 Docker 镜像，镜像中的**模型权重**适用其自身许可证，与本项目的 MIT 无关
3. 若你修改了 psycopg / ChromaDB 等依赖的源码，需遵守其对应许可证
4. 本声明基于各组件在 PyPI / 官方仓库公示的许可证信息整理，
   **商业发布前建议由法务复核**（尤其是 LGPL 与模型权重分发条款）

---

## 六、更新方式

依赖升级或新增后，请同步更新本文件。核对命令：

```bash
pip list --format=json
```

许可证信息可查 PyPI 项目页的 `License` 字段或源码仓库的 `LICENSE` 文件。
