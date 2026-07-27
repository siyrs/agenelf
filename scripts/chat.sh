#!/usr/bin/env bash
# chat.sh — CLI 入口便捷封装
# 用法：bash scripts/chat.sh [透传给 cli.py 的参数...]
#
# CLI 运行在独立的 cli 服务（profile=cli）中：它是唯一挂载审批 HMAC 密钥的
# 模型侧进程，因此签名主人命令只能从这里（或宿主机）发出；常驻 agenelf 容器
# 不再挂载 approval-key，无法自签审批。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

if ! command -v docker >/dev/null 2>&1; then
    echo "[chat] 错误：未找到 docker，请先安装并启动 Docker" >&2
    exit 1
fi

# cli.py 统一负责一次性认领重启续跑检查点。把宿主机的跳过开关显式传入
# 容器，避免 shell 包装层与 CLI 各执行一次续跑。
# MSYS_NO_PATHCONV=1 防止 Git Bash 把容器绝对路径转成 Windows 路径。
# docker compose run 默认分配 TTY 并保持 stdin 打开，交互体验与 exec 一致。
exec env MSYS_NO_PATHCONV=1 docker compose --profile cli run --rm \
    -e AGENELF_SKIP_AUTO_RESUME="${AGENELF_SKIP_AUTO_RESUME:-0}" \
    cli python /agenelf/app-fork/cli.py "$@"
