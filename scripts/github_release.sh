#!/usr/bin/env bash
# github_release.sh — 打版本注解标签并推送，可选通过 gh CLI 创建 GitHub Release
#
# 执行者：宿主机上的人类。
#
# 用法：bash scripts/github_release.sh <版本号>
#   版本号形如 0.2.0（自动补 v 前缀，写成 v0.2.0 也可以）。
#
# 行为：
#   1. 校验工作区干净（无未提交/未暂存/未跟踪文件）；
#   2. git tag -a v<x.y.z>，注解内容取 logs/evolution.log 最近 20 行
#      （文件不存在则用通用发布说明）；
#   3. 推送标签到 origin（失败给诊断，返回非 0，标签保留在本地）；
#   4. 若 gh CLI 可用则 gh release create 附说明，否则提示去 GitHub 网页创建。
#
# 退出码：0 成功；1 参数/环境错误；2 未配置远程；3 推送失败。
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

usage() {
    echo "用法：bash scripts/github_release.sh <版本号>（如 0.2.0，自动补 v 前缀）" >&2
}

# ---------- 0. 参数校验 ----------
VERSION="${1:-}"
if [[ -z "${VERSION}" ]]; then
    echo "[github_release] 错误：缺少版本号" >&2
    usage
    exit 1
fi
VERSION="${VERSION#v}"
if [[ ! "${VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "[github_release] 错误：版本号格式非法：${VERSION}（应为 x.y.z，如 0.2.0）" >&2
    exit 1
fi
TAG="v${VERSION}"

if ! git -C "${ROOT_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    log "[github_release] 错误：${ROOT_DIR} 不是 git 仓库"
    exit 1
fi
cd "${ROOT_DIR}"

# ---------- 1. 校验工作区干净 ----------
if [[ -n "$(git status --porcelain)" ]]; then
    echo "[github_release] 错误：工作区不干净，请先提交或清理以下变更：" >&2
    git status --short >&2
    exit 1
fi
if ! git rev-parse HEAD >/dev/null 2>&1; then
    echo "[github_release] 错误：仓库尚无任何提交，无法打版本标签" >&2
    exit 1
fi
if git rev-parse "${TAG}" >/dev/null 2>&1; then
    echo "[github_release] 错误：标签 ${TAG} 已存在，请换一个版本号" >&2
    exit 1
fi

# ---------- 2. 生成发布说明并打注解标签 ----------
EVO_LOG="${ROOT_DIR}/logs/evolution.log"
if [[ -f "${EVO_LOG}" ]]; then
    NOTES="Agenelf ${TAG} 发布

近期演化日志（最近 20 行）：
$(tail -n 20 "${EVO_LOG}")"
else
    NOTES="Agenelf ${TAG} 发布"
fi

git tag -a "${TAG}" -m "${NOTES}"
log "[github_release] 已创建注解标签 ${TAG}"

# ---------- 3. 推送标签 ----------
if ! git remote get-url origin >/dev/null 2>&1; then
    log "[github_release] 错误：未配置 origin 远程，标签仅保留在本地。"
    log "[github_release] 诊断：请先运行 bash scripts/github_setup.sh <仓库URL> 配置远程"
    exit 2
fi

PUSH_ERR="$(mktemp)"
trap 'rm -f "${PUSH_ERR}"' EXIT
if ! GIT_TERMINAL_PROMPT=0 git push origin "${TAG}" 2>"${PUSH_ERR}"; then
    err="$(cat "${PUSH_ERR}")"
    log "[github_release] 推送标签失败，标签已保留在本地，可稍后执行 git push origin ${TAG} 重试"
    if grep -qiE 'Could not resolve hostname|Connection (refused|timed out|reset)|Network is unreachable|Failed to connect|Temporary failure in name resolution' <<<"${err}"; then
        log "[github_release] 诊断：网络不可达，请检查网络连接/代理设置后重试"
    elif grep -qiE 'Authentication failed|Permission denied|403|401|could not read Username|terminal prompts disabled|invalid credentials' <<<"${err}"; then
        log "[github_release] 诊断：凭据问题，请检查 .env 中的 GITHUB_TOKEN（https）或 ssh 密钥配置"
    else
        log "[github_release] 诊断：未识别的推送错误，原始输出：${err}"
    fi
    exit 3
fi
log "[github_release] 标签 ${TAG} 已推送到 origin"

# ---------- 4. 创建 GitHub Release（可选） ----------
if command -v gh >/dev/null 2>&1; then
    if gh release create "${TAG}" --title "${TAG}" --notes "${NOTES}"; then
        log "[github_release] 已通过 gh CLI 创建 GitHub Release：${TAG}"
    else
        log "[github_release] 警告：gh release create 失败，请到 GitHub 网页基于标签 ${TAG} 手动创建 Release"
    fi
else
    log "[github_release] 提示：未检测到 gh CLI，请到 GitHub 网页基于标签 ${TAG} 创建 Release（说明可复用标签注解内容）"
fi

log "[github_release] 完成：${TAG}"
