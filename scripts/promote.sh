#!/usr/bin/env bash
# promote.sh — 将 app-tmp/ 中通过底线检查的改动晋升到 app/（真理之源）
#
# 执行者：宿主机上的人类或 watcher.sh（agent 无权在宿主机执行）。
#
# 流程：
#   1. 校验 data/promote-requests/<ID>/READY 存在且 report.txt 全部通过；
#   2. 备份 app/ 到 data/app-backups/<时间戳>.tar.gz；
#   3. rsync app-tmp/ -> app/（删除多余文件，排除 __pycache__）；
#   4. 调用 sync_fork.sh 刷新运行时副本；
#   5. docker compose restart（docker 不可用时跳过并提示）；
#   6. 写日志、清理请求目录。
# 第 3~4 步任一失败：自动从备份回滚 app/。
#
# 用法：bash scripts/promote.sh <请求ID>
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

APP_DIR="${ROOT_DIR}/app"
APP_TMP="${ROOT_DIR}/app-tmp"
LOG_FILE="${ROOT_DIR}/logs/evolution.log"
BACKUP_DIR="${ROOT_DIR}/data/app-backups"

REQ_ID="${1:-}"
if [[ -z "${REQ_ID}" ]]; then
    echo "[promote] 错误：缺少请求ID。用法：bash scripts/promote.sh <请求ID>" >&2
    exit 1
fi
# 请求ID合法性校验（防路径穿越）
if [[ ! "${REQ_ID}" =~ ^[a-zA-Z0-9._-]+$ ]]; then
    echo "[promote] 错误：请求ID含有非法字符：${REQ_ID}" >&2
    exit 1
fi

REQ_DIR="${ROOT_DIR}/data/promote-requests/${REQ_ID}"
mkdir -p "${ROOT_DIR}/logs" "${BACKUP_DIR}"

log() {
    local m="[$(date '+%F %T')] $*"
    echo "${m}"
    echo "${m}" >> "${LOG_FILE}"
}

log "[promote] ===== 开始晋升，请求ID：${REQ_ID} ====="

# ---------- 1. 校验 READY 标记与报告 ----------
if [[ ! -f "${REQ_DIR}/READY" ]]; then
    log "[promote] 错误：${REQ_DIR}/READY 不存在，该请求未通过底线检查（或不存在）"
    exit 1
fi
if [[ ! -f "${REQ_DIR}/report.txt" ]] || grep -q '^\[.*\] \[FAIL\]' "${REQ_DIR}/report.txt"; then
    log "[promote] 错误：report.txt 缺失或含未通过项，拒绝晋升"
    exit 1
fi
log "[promote] READY 标记与报告校验通过"

# ---------- 2. 备份 app/ ----------
TS="$(date +%Y%m%d-%H%M%S)"
BACKUP_FILE="${BACKUP_DIR}/${TS}.tar.gz"
tar -czf "${BACKUP_FILE}" -C "${ROOT_DIR}" app
log "[promote] 已备份 app/ -> ${BACKUP_FILE}"

# 回滚：从备份恢复 app/
rollback() {
    log "[promote] 发生失败，正在从备份回滚 app/ ..."
    rm -rf "${APP_DIR}"
    tar -xzf "${BACKUP_FILE}" -C "${ROOT_DIR}"
    log "[promote] 回滚完成，app/ 已恢复到晋升前状态"
}

# ---------- 3. rsync app-tmp/ -> app/ ----------
log "[promote] 同步 app-tmp/ -> app/"
if ! rsync -a --delete --exclude='__pycache__' --exclude='*.pyc' "${APP_TMP}/" "${APP_DIR}/"; then
    rollback
    exit 1
fi

# ---------- 4. 刷新运行时副本 app-fork/ ----------
if ! bash "${SCRIPT_DIR}/sync_fork.sh" >> "${LOG_FILE}" 2>&1; then
    log "[promote] sync_fork.sh 执行失败"
    rollback
    # 回滚后同样需要刷新 fork，尽力而为
    bash "${SCRIPT_DIR}/sync_fork.sh" >> "${LOG_FILE}" 2>&1 || true
    exit 1
fi
log "[promote] 运行时副本 app-fork/ 已刷新"

# ---------- 5. 重启容器 ----------
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    if (cd "${ROOT_DIR}" && docker compose restart); then
        log "[promote] 容器已重启，新代码生效"
    else
        log "[promote] 警告：docker compose restart 失败，请人工检查容器状态"
    fi
else
    log "[promote] 提示：当前环境 docker 不可用，跳过容器重启；请在部署机手动执行 docker compose restart"
fi

# ---------- 6. 收尾 ----------
log "[promote] ===== 晋升完成：${REQ_ID}（备份：${BACKUP_FILE}）====="
rm -rf "${REQ_DIR}"
log "[promote] 已清理请求目录 ${REQ_DIR}"

# ---------- 7. GitHub 自动备份钩子（可选，仅在晋升成功后触发） ----------
# 项目根 .env 含 GITHUB_AUTO_BACKUP=1 时，调用 github_backup.sh 推送状态备份；
# 备份失败仅警告，不影响晋升结果；脚本不存在时容错跳过。
if [[ -f "${ROOT_DIR}/.env" ]] && grep -qE '^\s*GITHUB_AUTO_BACKUP\s*=\s*1\s*$' "${ROOT_DIR}/.env"; then
    if [[ -f "${SCRIPT_DIR}/github_backup.sh" ]]; then
        log "[promote] 检测到 GITHUB_AUTO_BACKUP=1，执行 GitHub 状态备份 ..."
        if bash "${SCRIPT_DIR}/github_backup.sh" "auto: 晋升 ${REQ_ID} 后的状态备份"; then
            log "[promote] GitHub 状态备份完成"
        else
            log "[promote] 警告：GitHub 状态备份失败（不影响晋升结果），详见 logs/github.log"
        fi
    else
        log "[promote] 提示：scripts/github_backup.sh 不存在，跳过 GitHub 自动备份"
    fi
fi
exit 0
