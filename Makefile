# Agenelf 运维快捷命令（人类专用）
# 用法：make <目标>；详见 make help

.PHONY: help start stop restart build chat test backup promote logs status clean

# 默认目标：显示帮助
help: ## 显示全部可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

start: ## 同步 fork 并启动容器（首次会自动构建）
	bash scripts/sync_fork.sh
	docker compose up -d --build

stop: ## 紧急制动：停止容器
	docker compose stop

restart: ## 重启容器
	docker compose restart

build: ## 重新构建镜像
	docker compose build

chat: ## 进入 CLI 对话入口
	bash scripts/chat.sh

test: ## 运行全部单元测试
	cd app && python -m unittest discover -s tests

backup: ## 手动备份推送到 GitHub
	bash scripts/github_backup.sh

promote: ## 手动执行晋升（用法：make promote REQ=<请求ID>）
	bash scripts/promote.sh $(REQ)

watch: ## 启动晋升守护进程（后台自动执行晋升）
	nohup bash scripts/watcher.sh > logs/watcher.out 2>&1 &

logs: ## 查看进化日志
	tail -50 logs/evolution.log

status: ## 查看容器与晋升管道状态
	-docker compose ps
	-ls data/promote-requests/ 2>/dev/null || echo "（无待处理晋升请求）"

clean: ## 清空暂存区与待处理请求（危险操作，需确认）
	@echo "将清空 app-tmp/ 与 data/promote-requests/，5 秒内 Ctrl+C 取消..."; sleep 5
	rm -rf app-tmp/* data/promote-requests/*
	@echo "已清空"
