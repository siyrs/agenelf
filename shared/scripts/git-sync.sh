#!/usr/bin/env bash
# git-sync.sh — 安全提交、同步并推送当前分支。
#
# 用法：bash shared/scripts/git-sync.sh "提交说明"
# 不会强推；若远端合并产生冲突会立即停止并保留现场供人工处理。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT_DIR}"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "错误：${ROOT_DIR} 不是 Git 仓库" >&2
    exit 1
fi
if git rev-parse -q --verify MERGE_HEAD >/dev/null || \
   [[ -d "$(git rev-parse --git-path rebase-merge)" ]] || \
   [[ -d "$(git rev-parse --git-path rebase-apply)" ]]; then
    echo "错误：存在未完成的 merge/rebase，必须先人工处理" >&2
    exit 1
fi
if [[ -n "$(git ls-files -u)" ]]; then
    echo "错误：存在未解决的冲突文件，必须先人工处理" >&2
    exit 1
fi
if ! git remote get-url origin >/dev/null 2>&1; then
    echo "错误：未配置 origin 远程" >&2
    exit 1
fi

BRANCH="$(git symbolic-ref --quiet --short HEAD)" || {
    echo "错误：当前处于 detached HEAD，无法安全推送" >&2
    exit 1
}
MESSAGE="${1:-chore: sync ${BRANCH}}"

git add -A

# 防止把实际密钥文件或私钥误纳入本次提交；示例配置 .env.example 不受影响。
SENSITIVE_FILES="$(git diff --cached --name-only | grep -E '(^|/)(\.env|[^/]+\.(pem|key))$' || true)"
if [[ -n "${SENSITIVE_FILES}" ]]; then
    echo "错误：暂存区包含敏感文件，已停止同步：" >&2
    echo "${SENSITIVE_FILES}" >&2
    exit 1
fi
git diff --cached --check

COMMIT_CREATED="no"
if ! git diff --cached --quiet; then
    git commit -m "${MESSAGE}"
    COMMIT_CREATED="yes"
fi

git fetch origin
REMOTE_MERGED="no remote branch"
if git show-ref --verify --quiet "refs/remotes/origin/${BRANCH}"; then
    if ! git merge --no-edit "origin/${BRANCH}"; then
        echo "错误：远端合并产生冲突，已停止；请人工解决后再同步" >&2
        exit 1
    fi
    REMOTE_MERGED="origin/${BRANCH}"
fi

git push -u origin "${BRANCH}"
LOCAL_HEAD="$(git rev-parse HEAD)"
REMOTE_HEAD="$(git ls-remote origin "refs/heads/${BRANCH}" | awk '{print $1}')"
if [[ -z "${REMOTE_HEAD}" || "${LOCAL_HEAD}" != "${REMOTE_HEAD}" ]]; then
    echo "错误：推送后本地 HEAD 与 origin/${BRANCH} 不一致" >&2
    exit 1
fi

echo "Branch: ${BRANCH}"
echo "Commit created: ${COMMIT_CREATED}"
echo "Commit hash: ${LOCAL_HEAD}"
echo "Remote merged: ${REMOTE_MERGED}"
echo "Pushed to: origin/${BRANCH}"
echo "Notes: 工作区已同步，且本地 HEAD 与远端分支一致。"
