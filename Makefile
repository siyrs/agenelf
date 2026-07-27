# Agenelf operator commands — Node.js/TypeScript is the default Agent/API/CLI.
.PHONY: help init local mind models workflow validation repair start stop restart build chat legacy-chat node-test python-test test backup promote watch logs status ops approvals evolution autonomy approve deny clean

help: ## Show commands
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

init: ## Create/migrate private config, Node state, controlled queues and workspaces
	@test -f .env || cp .env.example .env
	@test -f .ops-runner.env || cp .ops-runner.env.example .ops-runner.env
	@python3 scripts/init_local.py
	@test -f local/node-runner.json || cp local/node-runner.example.json local/node-runner.json
	@mkdir -p logs workspace/scratch app-space/skills app-tmp app-fork code-workspaces repair-space \
		app-tmp/promote-requests \
		data/auth-requests data/auth-decisions data/auth-consumed \
		data/ops-requests data/ops-results data/ops-locks \
		data/approval-commands data/approval-results data/approval-locks \
		data/validation-requests data/validation-results data/validation-locks \
		data/repair-requests data/repair-results data/repair-locks \
		data/self-upgrade-requests data/self-upgrade-results data/self-upgrade-locks \
		data/self-upgrade-backups data/authorized-upgrades data/runner-health data/app-backups \
		data/tasks data/node-tasks data/channel-requests data/promote-requests data/promotion-history data/autonomy-cycles \
		data/node-runner-requests data/node-runner-results data/node-runner-locks
	@echo "初始化完成。默认 Agent/API/CLI 为 Node；Python legacy API 与安全 Runner 保留为内部兼容控制面。"

local: ## Validate local personalization without printing secrets
	@python3 scripts/init_local.py --status

mind: ## Show persistent reflection/intention files without dumping contents
	@echo "== local/self =="; ls -lh local/self 2>/dev/null || echo "尚未初始化，请运行 make init"

models: ## Show private model routing configuration status
	@python3 scripts/init_local.py --status | grep -E 'models|local_dir' || true

workflow: ## Show governed Python and Node workflow task files
	@echo "== Python workflow tasks =="; ls -lt data/tasks 2>/dev/null | head -20 || true
	@echo "== Node workflow tasks =="; ls -lt data/node-tasks 2>/dev/null | head -20 || true
	@echo "== channel requests =="; ls -lt data/channel-requests 2>/dev/null | head -20 || true

start: ## Start Node Agent/API plus internal Python compatibility API and deterministic runners
	@test -f .env || (echo "缺少 .env，请先 make init"; exit 1)
	@test -f .ops-runner.env || (echo "缺少 .ops-runner.env，请先 make init"; exit 1)
	@test -f local/profile.yaml || (echo "缺少 local/profile.yaml，请先 make init"; exit 1)
	@test -f local/preferences.yaml || (echo "缺少 local/preferences.yaml，请先 make init"; exit 1)
	@test -f local/servers.yaml || (echo "缺少 local/servers.yaml，请先 make init"; exit 1)
	@test -f local/validation.yaml || (echo "缺少 local/validation.yaml，请先 make init"; exit 1)
	@test -f local/models.yaml || (echo "缺少 local/models.yaml，请先 make init"; exit 1)
	@test -f local/repositories.yaml || (echo "缺少 local/repositories.yaml，请先 make init"; exit 1)
	@test -f local/node-runner.json || (echo "缺少 local/node-runner.json，请先 make init"; exit 1)
	@test -d local/memory || (echo "缺少 local/memory，请先 make init"; exit 1)
	@test -d local/self || (echo "缺少 local/self，请先 make init"; exit 1)
	@test -d local/secrets || (echo "缺少 local/secrets，请先 make init"; exit 1)
	@mkdir -p app-tmp/promote-requests \
		data/approval-commands data/approval-results data/approval-locks data/auth-decisions data/auth-consumed \
		data/ops-requests data/ops-results data/ops-locks \
		data/validation-requests data/validation-results data/validation-locks \
		data/repair-requests data/repair-results data/repair-locks \
		data/self-upgrade-requests data/self-upgrade-results data/self-upgrade-locks \
		data/promote-requests data/promotion-history data/runner-health data/node-tasks \
		data/node-runner-requests data/node-runner-results data/node-runner-locks
	bash scripts/sync_fork.sh
	docker compose up -d --build

stop: ## Stop all services
	docker compose stop

restart: ## Restart all services
	docker compose restart

build: ## Rebuild Node, legacy Python and runner images
	docker compose build

chat: ## Open the default Node CLI chat
	docker compose --profile cli run --rm cli

legacy-chat: ## Open the legacy Python CLI for migration diagnostics
	docker compose --profile legacy-cli run --rm legacy-cli

node-test: ## Run native TypeScript checks and Node tests
	npm ci --ignore-scripts
	npm run test:node

python-test: ## Run the retained Python governance and regression suite
	python3 scripts/validate_governance.py
	python3 -m compileall -q app scripts
	cd app && python -m unittest discover -s tests -v

test: node-test python-test ## Run complete Node and Python migration regression

backup: ## Back up generic source state to GitHub; local remains private
	bash scripts/github_backup.sh

promote: ## Promote an exact evolution request: make promote REQ=<evo-id>
	@test -n "$(REQ)" || (echo "用法：make promote REQ=evo-..."; exit 2)
	bash scripts/promote.sh $(REQ)

watch: ## Start watcher; notification-only unless explicitly enabled in .env
	@mkdir -p logs
	@if pgrep -f "scripts/watcher.sh" >/dev/null 2>&1; then \
		echo "watcher 已在运行，跳过重复启动"; \
	else \
		nohup bash scripts/watcher.sh > logs/watcher.out 2>&1 & \
		echo "watcher 已启动（日志：logs/watcher.out）"; \
	fi

evolution: ## Show evolution requests and immutable promotion evidence
	@echo "== staging requests (agent 可写，待宿主复核) =="; find app-tmp/promote-requests -maxdepth 2 -type f 2>/dev/null | sort || true
	@echo "== trusted promotion requests =="; find data/promote-requests -maxdepth 2 -type f 2>/dev/null | sort || true
	@echo "== promotion history =="; find data/promotion-history -maxdepth 2 -type f 2>/dev/null | sort | tail -40 || true

autonomy: ## Show recent controlled autonomy-cycle records
	@ls -lt data/autonomy-cycles 2>/dev/null | head -20 || echo "暂无自主循环记录"

approve: ## Cross-platform exact approval: make approve REQ=<op-id>
	@test -n "$(REQ)" || (echo "用法：make approve REQ=op-..."; exit 2)
	python3 scripts/approve.py $(REQ) approve

deny: ## Cross-platform exact denial: make deny REQ=<op-id> REASON='...'
	@test -n "$(REQ)" || (echo "用法：make deny REQ=op-... REASON='...'"; exit 2)
	python3 scripts/approve.py $(REQ) deny "$(REASON)"

approvals: ## Show signed owner approval commands, decisions and broker results
	@echo "== commands =="; ls -lt data/approval-commands 2>/dev/null | head -20 || true
	@echo "== decisions =="; ls -lt data/auth-decisions 2>/dev/null | head -20 || true
	@echo "== broker results =="; ls -lt data/approval-results 2>/dev/null | head -20 || true

ops: ## Show recent operation requests and results
	@echo "== requests =="; ls -lt data/ops-requests 2>/dev/null | head -20 || true
	@echo "== decisions =="; ls -lt data/auth-decisions 2>/dev/null | head -20 || true
	@echo "== results =="; ls -lt data/ops-results 2>/dev/null | head -20 || true

validation: ## Show validation configuration and trusted queues
	@echo "== validation config =="; python3 scripts/init_local.py --status | grep -E 'validation|local_dir' || true
	@echo "== validation requests =="; ls -lt data/validation-requests 2>/dev/null | head -20 || true
	@echo "== validation results =="; ls -lt data/validation-results 2>/dev/null | head -20 || true

repair: ## Show code repair aliases, queues and evidence
	@echo "== repositories config =="; python3 scripts/init_local.py --status | grep -E 'repositories|code_workspaces|repair_space' || true
	@echo "== repair requests =="; ls -lt data/repair-requests 2>/dev/null | head -20 || true
	@echo "== repair results =="; ls -lt data/repair-results 2>/dev/null | head -20 || true
	@echo "== repair artifacts =="; ls -lt repair-space 2>/dev/null | head -20 || true

logs: ## Follow Node Agent, internal legacy API and deterministic runners
	docker compose logs -f --tail=100 agenelf legacy-agent approval-runner ops-runner validation-runner repair-runner self-upgrade-runner

status: ## Show containers, local state and all controlled queues
	-docker compose ps
	@$(MAKE) --no-print-directory local
	@$(MAKE) --no-print-directory mind
	@$(MAKE) --no-print-directory models
	@$(MAKE) --no-print-directory workflow
	@$(MAKE) --no-print-directory ops
	@$(MAKE) --no-print-directory approvals
	@$(MAKE) --no-print-directory validation
	@$(MAKE) --no-print-directory repair
	@$(MAKE) --no-print-directory autonomy

clean: ## Clear transient queues; preserve owner data and trusted result evidence
	@echo "将清空 app-tmp、未完成请求和锁，5 秒内 Ctrl+C 取消..."; sleep 5
	rm -rf app-tmp/* data/ops-requests/* data/ops-locks/* \
		data/approval-commands/* data/approval-locks/* \
		data/validation-requests/* data/validation-locks/* \
		data/repair-requests/* data/repair-locks/* \
		data/node-runner-requests/* data/node-runner-locks/*
	@echo "已清空；local/、Node/Python 结果证据、repair-space、自主循环、裁决与晋升证据均保留。"
