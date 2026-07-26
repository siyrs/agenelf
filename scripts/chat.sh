#!/usr/bin/env bash
# chat.sh — CLI 入口便捷封装
# 用法：bash scripts/chat.sh [透传给 cli.py 的参数...]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

if ! command -v docker >/dev/null 2>&1; then
    echo "[chat] 错误：未找到 docker，请先安装并启动 Docker" >&2
    exit 1
fi

# 先尝试恢复主人在升级/重启前明确保存的任务检查点。-T 禁止分配 TTY，
# 避免自动续跑与随后打开的交互 CLI 争用终端；失败不阻断主人进入 CLI。
if [[ "${AGENELF_SKIP_AUTO_RESUME:-0}" != "1" ]]; then
    if ! env MSYS_NO_PATHCONV=1 docker compose exec -T agenelf \
        python /agenelf/app-fork/resume.py; then
        echo "[chat] 警告：自动续跑失败，继续进入交互 CLI" >&2
    fi
fi

# 注意：容器内运行的是只读挂载的 app-fork/ 代码
# MSYS_NO_PATHCONV=1 防止 Git Bash 把容器绝对路径转成 Windows 路径
exec env MSYS_NO_PATHCONV=1 docker compose exec agenelf \
    python /agenelf/app-fork/cli.py "$@"
