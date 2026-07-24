# Agenelf operator commands
.PHONY: help init local mind validation start stop restart build chat test backup promote watch logs status ops evolution autonomy approve clean

help: ## Show commands
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

init: ## Create/migrate local personalization, continuity and runtime directories
	@test -f .env || cp .env.example .env
	@test -f .ops-runner.env || cp .ops-runner.env.example .ops-runner.env
	@python3 scripts/init_local.py
	@mkdir -p logs workspace app-space app-tmp app-fork \
		data/auth-requests data/auth-decisions data/auth-consumed \
		data/ops-requests data/ops-results data/ops-locks \
		data/validation-requests data/validation-results data/validation-locks \
		data/promote-requests data/promotion-history data/autonomy-cycles
	@echo "初始化完成。请编辑 local/ 配置；长期记忆在 local/memory，成长连续性在 local/self。"

local: ## Validate local personalization without printing secrets
	@python3 scripts/init_local.py --status

mind: ## Show persistent reflection/intention files without dumping their contents
	@echo "== local/self =="; ls -lh local/self 2>/dev/null || echo "尚未初始化，请运行 make init"

start: ## Sync runtime fork and start Agent + deterministic ops runner
	@test -f .env || (echo "缺少 .env，请先 make init"; exit 1)
	@test -f .ops-runner.env || (echo "缺少 .ops-runner.env，请先 make init"; exit 1)
	@test -f local/profile.yaml || (echo "缺少 local/profile.yaml，请先 make init"; exit 1)
	@test -f local/preferences.yaml || (echo "缺少 local/preferences.yaml，请先 make init"; exit 1)
	@test -f local/servers.yaml || (echo "缺少 local/servers.yaml，请先 make init"; exit 1)
	@test -f local/validation.yaml || (echo "缺少 local/validation.yaml，请先 make init"; exit 1)
	@test -d local/memory || (echo "缺少 local/memory，请先 make init"; exit 1)
	@test -d local/self || (echo "缺少 local/self，请先 make init"; exit 1)
	@test -d local/secrets || (echo "缺少 local/secrets，请先 make init"; exit 1)
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

backup: ## Back up generic app source state to GitHub; local remains private
	bash scripts/github_backup.sh

promote: ## Promote an exact evolution request: make promote REQ=<evo-id>
	@test -n "$(REQ)" || (echo "用法：make promote REQ=evo-..."; exit 2)
	bash scripts/promote.sh $(REQ)

watch: ## Start watcher; notification-only unless explicitly enabled in .env
	nohup bash scripts/watcher.sh > logs/watcher.out 2>&1 &

evolution: ## Show evolution requests and immutable promotion evidence
	@echo "== promotion requests =="; find data/promote-requests -maxdepth 2 -type f 2>/dev/null | sort || true
	@echo "== promotion history =="; find data/promotion-history -maxdepth 2 -type f 2>/dev/null | sort | tail -40 || true

autonomy: ## Show recent controlled autonomy-cycle records
	@ls -lt data/autonomy-cycles 2>/dev/null | head -20 || echo "暂无自主循环记录"

approve: ## Approve an exact server operation: make approve REQ=<op-id>
	@test -n "$(REQ)" || (echo "用法：make approve REQ=op-..."; exit 2)
	bash scripts/approve.sh $(REQ) approve

ops: ## Show recent operation requests and results
	@echo "== requests =="; ls -lt data/ops-requests 2>/dev/null | head -20 || true
	@echo "== decisions =="; ls -lt data/auth-decisions 2>/dev/null | head -20 || true
	@echo "== results =="; ls -lt data/ops-results 2>/dev/null | head -20 || true

validation: ## Show validation configuration and trusted queues
	@echo "== validation config =="; python3 scripts/init_local.py --status | grep -E 'validation|local_dir' || true
	@echo "== validation requests =="; ls -lt data/validation-requests 2>/dev/null | head -20 || true
	@echo "== validation results =="; ls -lt data/validation-results 2>/dev/null | head -20 || true

logs: ## Follow Agent and deterministic runners
	docker compose logs -f --tail=100 agenelf ops-runner validation-runner

status: ## Show containers, local state and all controlled queues
	-docker compose ps
	@$(MAKE) --no-print-directory local
	@$(MAKE) --no-print-directory mind
	@$(MAKE) --no-print-directory ops
	@$(MAKE) --no-print-directory validation
	@$(MAKE) --no-print-directory autonomy

clean: ## Clear temporary task queues; never delete local owner or continuity data
	@echo "将清空 app-tmp 和运行队列，5 秒内 Ctrl+C 取消..."; sleep 5
	rm -rf app-tmp/* data/ops-requests/* data/ops-results/* data/ops-locks/* \
		data/validation-requests/* data/validation-locks/*
	@echo "已清空；local/、自我沉淀、自主循环、裁决与晋升证据均保留。"
