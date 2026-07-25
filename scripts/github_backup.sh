#!/usr/bin/env bash
# github_backup.sh — 将 app/ 与 scripts/ 的变更提交并推送到 origin，附 backup/<时间戳> 标签
#
# 执行者：宿主机上的人类或 promote.sh 的自动备份钩子。
#
# 用法：bash scripts/github_backup.sh [提交信息]
#
# 行为：
#   1. 仅暂存并提交 app/ 与 scripts/ 的变更（无变更则跳过并提示，返回 0）；
#   2. 打标签 backup/<时间戳>；
#   3. 推送当前分支与该标签到 origin；
#   4. 推送失败不炸脚本：返回非 0，并给出诊断（无远程 / 网络 / 凭据）；
#   5. 全程日志追加到 logs/github.log。
#
# 退出码：0 成功（含无变更跳过）；1 环境错误；2 未配置远程；3 推送失败。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
LOG_DIR="${ROOT_DIR}/logs"
LOG_FILE="${LOG_DIR}/github.log"
mkdir -p "${LOG_DIR}"

log() {
    local m
    m="[$(date '+%F %T')] $*"
    echo "${m}"
    echo "${m}" >> "${LOG_FILE}"
}

COMMIT_MSG="${1:-backup: 状态备份}"

# ---------- 0. 校验 git 仓库 ----------
if ! git -C "${ROOT_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    log "[github_backup] 错误：${ROOT_DIR} 不是 git 仓库，请先执行 git init"
    exit 1
fi
cd "${ROOT_DIR}"

# ---------- 1. 暂存并提交 app/ 与 scripts/ ----------
git add app scripts
if git diff --cached --quiet; then
    log "[github_backup] app/ 与 scripts/ 无变更，跳过备份"
    exit 0
fi
if ! git commit -m "${COMMIT_MSG}" >/dev/null; then
    log "[github_backup] 错误：git commit 失败（请检查 user.name/user.email 配置）"
    exit 1
fi
log "[github_backup] 已提交：${COMMIT_MSG}"

# ---------- 2. 打 backup/<时间戳> 标签 ----------
TS="$(date +%Y%m%d-%H%M%S)"
TAG="backup/${TS}"
git tag "${TAG}"
log "[github_backup] 已打标签 ${TAG}"

# ---------- 3. 检查远程 ----------
if ! git remote get-url origin >/dev/null 2>&1; then
    log "[github_backup] 错误：未配置 origin 远程，提交与标签仅保留在本地。"
    log "[github_backup] 诊断：请先运行 bash scripts/github_setup.sh <仓库URL> 配置远程"
    exit 2
fi

BRANCH="$(git symbolic-ref --short HEAD 2>/dev/null || echo HEAD)"

# ---------- 4. 推送（失败不炸，给出诊断） ----------
PUSH_ERR="$(mktemp)"
trap 'rm -f "${PUSH_ERR}"' EXIT
push_failed=0
GIT_TERMINAL_PROMPT=0 git push -u origin "${BRANCH}" 2>"${PUSH_ERR}" || push_failed=1
if [[ ${push_failed} -eq 0 ]]; then
    GIT_TERMINAL_PROMPT=0 git push origin "${TAG}" 2>>"${PUSH_ERR}" || push_failed=1
fi

if [[ ${push_failed} -ne 0 ]]; then
    err="$(cat "${PUSH_ERR}")"
    log "[github_backup] 推送失败，提交与标签已保留在本地，可稍后重试"
    if grep -qiE 'Could not resolve hostname|Connection (refused|timed out|reset)|Network is unreachable|Failed to connect|Temporary failure in name resolution' <<<"${err}"; then
        log "[github_backup] 诊断：网络不可达，请检查网络连接/代理设置后重试"
    elif grep -qiE 'Authentication failed|Permission denied|403|401|could not read Username|terminal prompts disabled|invalid credentials' <<<"${err}"; then
        log "[github_backup] 诊断：凭据问题，请检查 .env 中的 GITHUB_TOKEN（https）或 ssh 密钥配置"
    else
        log "[github_backup] 诊断：未识别的推送错误，原始输出：${err}"
    fi
    exit 3
fi

log "[github_backup] 推送完成：分支 ${BRANCH} + 标签 ${TAG} -> origin"
