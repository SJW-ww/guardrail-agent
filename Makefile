SHELL := /bin/bash
COMPOSE := docker compose
API_DIR := apps/api

-include .env
export

POSTGRES_USER ?= guardrail
POSTGRES_PASSWORD ?= guardrail
POSTGRES_DB ?= guardrail
TEST_DATABASE_URL ?= postgresql+asyncpg://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@postgres:5432/guardrail_test

.PHONY: help init up down logs ps restart migrate migrate-local revision seed test-integration api-check api-test api-lint api-fmt web-dev web-check check clean worker demo-governance demo-planner

help:  ## 显示所有可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

init:  ## 初始化本地配置(生成 .env)
	@test -f .env || cp .env.example .env
	@echo ".env ready"

up:  ## 起全栈
	$(COMPOSE) up -d --build

down:  ## 停全栈(保留数据卷)
	$(COMPOSE) down

logs:  ## 跟踪日志
	$(COMPOSE) logs -f --tail=100

ps:  ## 查看容器状态
	$(COMPOSE) ps

restart:  ## 重启 api
	$(COMPOSE) restart api

migrate:  ## 在容器内执行迁移(推荐,容器能直连 postgres)
	$(COMPOSE) exec -T api uv run alembic upgrade head

migrate-local:  ## 在本机执行迁移(需要本机能连 5432)
	cd $(API_DIR) && uv run alembic upgrade head

revision:  ## 新建迁移,用法: make revision m="add orders"
	@test -n "$(m)" || (echo 'usage: make revision m="message"' && exit 1)
	$(COMPOSE) exec -T api uv run alembic revision --autogenerate -m "$(m)"

seed:  ## 灌入演示数据
	$(COMPOSE) exec -T api uv run python -m guardrail_api.scripts.seed

seed-reset:  ## 清空并重建演示数据
	$(COMPOSE) exec -T api uv run python -m guardrail_api.scripts.seed --reset

test-integration:  ## 在容器内跑集成测试(需要 postgres)
	$(COMPOSE) exec -T -e TEST_DATABASE_URL="$(TEST_DATABASE_URL)" api uv run pytest -m integration -q

e2e:  ## 端到端冒烟测试(会先重置演示数据,需要整个栈在跑)
	$(COMPOSE) exec -T api uv run python -m guardrail_api.scripts.seed --reset
	npm run e2e -w @guardrail/web

worker:  ## 常驻执行 worker(治理层唯一有权推进 run 的进程之一)
	$(COMPOSE) exec -T api uv run python -m guardrail_api.governance.worker

demo-governance:  ## 演示 W2 三条验收:kill -9 续跑 · 幂等重放 · 审计前后值
	$(COMPOSE) exec -T api uv run python -m guardrail_api.scripts.demo_governance

demo-planner:  ## 演示 W3:真模型跑规划器(需在 .env 配好 LLM_*,会消耗 token)
	@test -n "$(LLM_API_KEY)" || (echo "请先在 .env 里配好 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL" && exit 1)
	$(COMPOSE) exec -T -e PLANNER_BACKEND=llm api \
		uv run python -m guardrail_api.scripts.demo_planner $(if $(i),-i "$(i)")

api-check: api-lint api-test  ## 后端静态检查 + 测试

api-lint:  ## ruff lint + format check
	cd $(API_DIR) && uv run ruff check . && uv run ruff format --check .

api-fmt:  ## ruff 自动修复 + 格式化
	cd $(API_DIR) && uv run ruff check --fix . && uv run ruff format .

api-test:  ## 跑后端测试(不含 integration)
	cd $(API_DIR) && uv run pytest -m "not integration"

web-dev:  ## 本地起前端(npm workspaces)
	npm run dev

web-check:  ## 前端 typecheck + lint
	npm run typecheck && npm run lint

check: api-check web-check  ## 跑全部检查(CI 等价)

clean:  ## 清缓存
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf $(API_DIR)/.pytest_cache $(API_DIR)/.ruff_cache
