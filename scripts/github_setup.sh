#!/usr/bin/env bash
# github_setup.sh — 配置 GitHub 远程仓库（remote origin）
#
# 执行者：宿主机上的人类（agent 无权执行 scripts/ 下脚本）。
#
# 用法：
#   bash scripts/github_setup.sh <仓库URL>   添加或更新 origin 远程地址
#   bash scripts/github_setup.sh --help      显示本帮助
#
# 行为：
#   1. 校验项目根是 git 仓库；
#   2. origin 不存在则 git remote add，已存在则 set-url 更新；
#   3. 检查 git user.name / user.email 配置，缺失时提示如何设置；
#   4. 提示推送凭据：https 推送可在 .env 写入 GITHUB_TOKEN，或改用 ssh 地址。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

usage() {
    cat <<'EOF'
用法：bash scripts/github_setup.sh <仓库URL>

配置 GitHub 远程仓库 origin：
  - origin 不存在则添加，已存在则更新为新的 URL；
  - 检查 git user.name / user.email 配置是否完整；
  - 提示推送凭据配置（.env 中 GITHUB_TOKEN 或 ssh 密钥）。

示例：
  bash scripts/github_setup.sh https://github.com/yourname/agenelf.git
  bash scripts/github_setup.sh git@github.com:yourname/agenelf.git
EOF
}

case "${1:-}" in
    -h|--help)
        usage
        exit 0
        ;;
esac

REPO_URL="${1:-}"
if [[ -z "${REPO_URL}" ]]; then
    echo "[github_setup] 错误：缺少仓库URL" >&2
    usage >&2
    exit 1
fi

# ---------- 1. 校验 git 仓库 ----------
if ! git -C "${ROOT_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "[github_setup] 错误：${ROOT_DIR} 不是 git 仓库，请先执行 git init" >&2
    exit 1
fi

# ---------- 2. 添加或更新 origin ----------
if git -C "${ROOT_DIR}" remote get-url origin >/dev/null 2>&1; then
    OLD_URL="$(git -C "${ROOT_DIR}" remote get-url origin)"
    git -C "${ROOT_DIR}" remote set-url origin "${REPO_URL}"
    echo "[github_setup] 已更新 origin：${OLD_URL} -> ${REPO_URL}"
else
    git -C "${ROOT_DIR}" remote add origin "${REPO_URL}"
    echo "[github_setup] 已添加 origin：${REPO_URL}"
fi
echo "[github_setup] 当前远程配置："
git -C "${ROOT_DIR}" remote -v

# ---------- 3. 检查 git 身份配置 ----------
if ! git -C "${ROOT_DIR}" config user.name >/dev/null 2>&1; then
    echo "[github_setup] 提示：未配置 user.name，提交前请执行：" >&2
    echo "  git config --global user.name \"你的名字\"" >&2
fi
if ! git -C "${ROOT_DIR}" config user.email >/dev/null 2>&1; then
    echo "[github_setup] 提示：未配置 user.email，提交前请执行：" >&2
    echo "  git config --global user.email \"you@example.com\"" >&2
fi

# ---------- 4. 推送凭据提示 ----------
ENV_FILE="${ROOT_DIR}/.env"
if [[ -f "${ENV_FILE}" ]] && grep -qE '^\s*GITHUB_TOKEN\s*=\s*\S+' "${ENV_FILE}"; then
    echo "[github_setup] 检测到 .env 已配置 GITHUB_TOKEN（https 推送可用）"
else
    echo "[github_setup] 提示：使用 https 地址推送时，可在 .env 中写入 GITHUB_TOKEN=<你的访问令牌>；"
    echo "  或改用 ssh 地址（git@github.com:...）并配置好 ssh 密钥，免输凭据。"
fi

echo "[github_setup] 完成"
