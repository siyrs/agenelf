#!/usr/bin/env bash
# chat.sh — CLI 入口便捷封装
# 等价于在宿主机执行：
#   docker compose exec agenelf python /agenelf/app-fork/cli.py
# 用法：bash scripts/chat.sh [透传给 cli.py 的参数...]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

if ! command -v docker >/dev/null 2>&1; then
    echo "[chat] 错误：未找到 docker，请先安装并启动 Docker" >&2
    exit 1
fi

# 注意：容器内运行的是只读挂载的 app-fork/ 代码
exec docker compose exec agenelf python /agenelf/app-fork/cli.py "$@"
