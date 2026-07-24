# Agenelf operator commands
.PHONY: help init start stop restart build chat test backup promote watch logs status ops approve clean

help: ## Show commands
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

init: ## Create local config skeleton and runtime directories
	@test -f .env || cp .env.example .env
	@test -f .ops-runner.env || cp .ops-runner.env.example .ops-runner.env
	@test -f config/servers.yaml || cp config/servers.example.yaml config/servers.yaml
	@mkdir -p secrets logs workspace app-space app-tmp app-fork \
		data/auth-requests data/auth-decisions data/auth-consumed \
		data/ops-requests data/ops-results data/ops-locks data/promote-requests
	@echo "初始化完成。请编辑 .env、.ops-runner.env、config/servers.yaml，并放入 secrets/。"

start: ## Sync runtime fork and start Agent + deterministic ops runner
	@test -f .env || (echo "缺少 .env，请先 make init"; exit 1)
	@test -f .ops-runner.env || (echo "缺少 .ops-runner.env，请先 make init"; exit 1)
	@test -f config/servers.yaml || (echo "缺少 config/servers.yaml，请先 make init"; exit 1)
	bash scripts/sync_fork.sh
	docker compose up -d --build

stop: ## Stop all services
	docker compose stop

restart: ## Restart all services
	docker compose restart

build: ## Rebuild images
	docker compose build

chat: ## Open CLI chat
	bash scripts/chat.sh

test: ## Run the complete unit test suite
	cd app && python -m unittest discover -s tests -v

backup: ## Back up source state to GitHub
	bash scripts/github_backup.sh

promote: ## Promote an evolution request: make promote REQ=<id>
	bash scripts/promote.sh $(REQ)

watch: ## Start the evolution promotion watcher
	nohup bash scripts/watcher.sh > logs/watcher.out 2>&1 &

approve: ## Approve an exact operation: make approve REQ=<op-id>
	@test -n "$(REQ)" || (echo "用法：make approve REQ=op-..."; exit 2)
	bash scripts/approve.sh $(REQ) approve

ops: ## Show recent operation requests and results
	@echo "== requests =="; ls -lt data/ops-requests 2>/dev/null | head -20 || true
	@echo "== decisions =="; ls -lt data/auth-decisions 2>/dev/null | head -20 || true
	@echo "== results =="; ls -lt data/ops-results 2>/dev/null | head -20 || true

logs: ## Follow Agent and ops-runner logs
	docker compose logs -f --tail=100 agenelf ops-runner

status: ## Show containers and queues
	-docker compose ps
	@$(MAKE) --no-print-directory ops

clean: ## Clear temporary/runtime queues after a five-second grace period
	@echo "将清空 app-tmp 和运行队列，5 秒内 Ctrl+C 取消..."; sleep 5
	rm -rf app-tmp/* data/ops-requests/* data/ops-results/* data/ops-locks/*
	@echo "已清空；人类裁决文件未自动删除。"
