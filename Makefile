# ============================================================================
# RAG 知识库问答系统 — 开发命令入口
# 所有命令都可用 `make help` 查看
# ============================================================================

.DEFAULT_GOAL := help
SHELL := /bin/bash

# ---------- 可覆盖变量 ----------
PYTHON ?= python
VENV   ?= .venv
BACKEND := backend
COMPOSE_DEV := docker compose -f docker-compose.dev.yml
COMPOSE     := docker compose

# Windows 下 venv 的路径不同
ifeq ($(OS),Windows_NT)
    VENV_BIN := $(VENV)/Scripts
else
    VENV_BIN := $(VENV)/bin
endif
PY := $(VENV_BIN)/python
PIP := $(VENV_BIN)/pip

.PHONY: help
help: ## 显示所有可用命令
	@echo ""
	@echo "  RAG 知识库问答系统 — 可用命令"
	@echo "  ────────────────────────────────────────────────"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'
	@echo ""

# ---------------------------------------------------------------------------
# 环境准备
# ---------------------------------------------------------------------------
.PHONY: venv
venv: ## 创建 Python 虚拟环境并安装依赖（含 dev）
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip setuptools wheel
	cd $(BACKEND) && ../$(PIP) install -e ".[dev]"
	@echo "✓ 虚拟环境就绪：$(VENV)"

.PHONY: env
env: ## 从模板生成 .env（已存在则跳过）
	@if [ -f .env ]; then \
		echo "⚠ .env 已存在，跳过。如需重置请手动删除。"; \
	else \
		cp .env.example .env; \
		echo "✓ 已生成 .env，请修改 JWT_SECRET / FERNET_KEY / 数据库密码"; \
	fi

.PHONY: env-key
env-key: ## 生成随机 JWT_SECRET 与 FERNET_KEY（打印到终端）
	@echo "JWT_SECRET=$$($(PY) -c 'import secrets;print(secrets.token_urlsafe(48))')"
	@echo "FERNET_KEY=$$($(PY) -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')"

# ---------------------------------------------------------------------------
# 基础设施（开发用）
# ---------------------------------------------------------------------------
.PHONY: db-up
db-up: ## 启动 PG + Redis（开发编排）
	$(COMPOSE_DEV) up -d
	@echo "等待数据库就绪..."
	@until docker exec kbqa-postgres pg_isready -U postgres -d kbqa >/dev/null 2>&1; do sleep 1; done
	@echo "✓ PG + Redis 已就绪"

.PHONY: db-down
db-down: ## 停止开发基础设施（保留数据）
	$(COMPOSE_DEV) down

.PHONY: db-reset
db-reset: ## 销毁并重建数据库（数据全丢，慎用）
	@echo "⚠ 将删除所有数据，5 秒内 Ctrl+C 可取消"; sleep 5
	$(COMPOSE_DEV) down -v
	$(COMPOSE_DEV) up -d
	@echo "✓ 已重置"

.PHONY: db-init
db-init: ## 建扩展、建表、建 RLS 策略与应用角色
	cd $(BACKEND) && ../$(PY) -m app.db.bootstrap --with-role

.PHONY: db-migrate
db-migrate: ## 执行 alembic 迁移到最新
	cd $(BACKEND) && ../$(VENV_BIN)/alembic upgrade head

# ---------------------------------------------------------------------------
# 开发运行
# ---------------------------------------------------------------------------
.PHONY: dev
dev: ## 启动后端（热重载）与索引 worker
	@echo "提示：问答与索引需要模型服务在运行（见 README 启动顺序）"
	$(PY) scripts/dev.py --host 127.0.0.1 --port 8080

.PHONY: worker
worker: ## 只启动索引 worker
	cd $(BACKEND) && ../$(PY) -m app.workers.ingest_worker --concurrency 2

.PHONY: web
web: ## 启动前端开发服务器
	cd frontend && npm run dev

# ---------------------------------------------------------------------------
# 测试与质量
# ---------------------------------------------------------------------------
.PHONY: test
test: ## 跑全部单元测试（不需要模型服务）
	cd $(BACKEND) && ../$(PY) -m pytest

.PHONY: test-db
test-db: ## 跑需要真实数据库的测试（需先 db-up + db-init）
	cd $(BACKEND) && ../$(PY) -m pytest -m db -v

.PHONY: verify-rls
verify-rls: ## 验证多租户 RLS 隔离是否真正生效（11 项检查）
	$(PY) scripts/verify_rls.py

.PHONY: test-parsers
test-parsers: ## 只跑解析层测试（不需要模型/数据库）
	cd $(BACKEND) && ../$(PY) -m pytest tests/parsers -v

.PHONY: parse-report
parse-report: ## 生成解析质量报告（对 eval/fixtures 全部文件跑一遍）
	$(PY) scripts/parse_report.py

.PHONY: parse-baseline
parse-baseline: ## 更新解析基线并检查回归
	$(PY) scripts/parse_report.py --baseline

.PHONY: fixtures
fixtures: ## 重新生成合成测试素材
	$(PY) scripts/make_fixtures.py

.PHONY: spike
spike: ## 竖切片端到端验证（需要 LM Studio 已加载 embedding + chat 模型）
	$(PY) scripts/spike.py

.PHONY: lint
lint: ## 代码检查
	cd $(BACKEND) && ../$(VENV_BIN)/ruff check .

.PHONY: fmt
fmt: ## 自动格式化与修复
	cd $(BACKEND) && ../$(VENV_BIN)/ruff check --fix . && ../$(VENV_BIN)/ruff format .

# ---------------------------------------------------------------------------
# 运维
# ---------------------------------------------------------------------------
.PHONY: reindex
reindex: ## 从 PG 全量重建 Chroma 与 BM25 索引
	cd $(BACKEND) && ../$(PY) -m app.services.reindex

.PHONY: reprocess
reprocess: ## 单文档单阶段重跑：make reprocess DOC=xxx STAGE=chunk
	@if [ -z "$(DOC)" ]; then echo "用法: make reprocess DOC=<document_id> STAGE=<parse|chunk|embed>"; exit 1; fi
	cd $(BACKEND) && ../$(PY) -m app.services.reprocess --doc $(DOC) --stage $(STAGE)

.PHONY: seed
seed: ## 导入示例知识库（需模型服务在运行）
	cd $(BACKEND) && ../$(PY) -m scripts.seed

.PHONY: backup
backup: ## 导出 PG + 原文件 + BM25 快照为一个 tar（不含向量，可用 reindex 重建）
	@mkdir -p backups
	@STAMP=$$(date +%Y%m%d-%H%M%S); \
	docker exec kbqa-postgres pg_dump -U postgres kbqa > backups/pg-$$STAMP.sql; \
	tar -czf backups/kbqa-$$STAMP.tar.gz backups/pg-$$STAMP.sql data/files data/bm25; \
	rm -f backups/pg-$$STAMP.sql; \
	echo "✓ backups/kbqa-$$STAMP.tar.gz"

.PHONY: diagram-check
diagram-check: ## 校验产品架构图的几何对齐
	$(PY) scripts/check-diagram.py

# ---------------------------------------------------------------------------
# 生产编排
# ---------------------------------------------------------------------------
.PHONY: up
up: ## 启动全套（生产编排，含 Chroma 服务）
	$(COMPOSE) up -d --build
	@echo "✓ 访问 http://localhost:3000"

.PHONY: down
down: ## 停止全套
	$(COMPOSE) down

.PHONY: logs
logs: ## 查看日志：make logs S=api
	$(COMPOSE) logs -f $(S)
