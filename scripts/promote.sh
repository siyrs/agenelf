#!/usr/bin/env bash
# promote.sh — promote an exact, gate-approved app-tmp tree into app/.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
APP_DIR="${ROOT_DIR}/app"
APP_TMP="${ROOT_DIR}/app-tmp"
LOG_FILE="${ROOT_DIR}/logs/evolution.log"
BACKUP_DIR="${ROOT_DIR}/data/app-backups"
TREE_DIGEST="${SCRIPT_DIR}/tree_digest.py"

REQ_ID="${1:-}"
if [[ -z "${REQ_ID}" ]]; then
    echo "[promote] 错误：缺少请求ID。用法：bash scripts/promote.sh <请求ID>" >&2
    exit 1
fi
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
if [[ ! -f "${REQ_DIR}/READY" ]]; then
    log "[promote] 错误：${REQ_DIR}/READY 不存在"
    exit 1
fi
if [[ ! -f "${REQ_DIR}/report.txt" ]] || grep -q '^\[.*\] \[FAIL\]' "${REQ_DIR}/report.txt"; then
    log "[promote] 错误：report.txt 缺失或含未通过项"
    exit 1
fi
if [[ ! -f "${REQ_DIR}/candidate.sha256" ]]; then
    log "[promote] 错误：candidate.sha256 缺失，旧版或不完整 READY 不可晋升"
    exit 1
fi
EXPECTED_SHA="$(tr -d '[:space:]' < "${REQ_DIR}/candidate.sha256")"
if [[ ! "${EXPECTED_SHA}" =~ ^[0-9a-f]{64}$ ]]; then
    log "[promote] 错误：候选摘要格式非法：${EXPECTED_SHA}"
    exit 1
fi
if [[ ! -f "${TREE_DIGEST}" ]]; then
    log "[promote] 错误：可信摘要脚本不存在：${TREE_DIGEST}"
    exit 1
fi
CURRENT_SHA="$(python3 "${TREE_DIGEST}" "${APP_TMP}")"
if [[ "${CURRENT_SHA}" != "${EXPECTED_SHA}" ]]; then
    log "[promote] 错误：候选代码在 gate 通过后发生变化"
    log "[promote] gate=${EXPECTED_SHA} current=${CURRENT_SHA}；拒绝时间差晋升"
    printf '拒绝原因：候选代码摘要变化，必须重新运行 gate_check.sh\n' > "${REQ_DIR}/REJECTED"
    rm -f "${REQ_DIR}/READY"
    exit 1
fi
log "[promote] READY、报告与候选摘要全部校验通过：${CURRENT_SHA}"

TS="$(date +%Y%m%d-%H%M%S)"
BACKUP_FILE="${BACKUP_DIR}/${TS}.tar.gz"
tar -czf "${BACKUP_FILE}" -C "${ROOT_DIR}" app
log "[promote] 已备份 app/ -> ${BACKUP_FILE}"
rollback() {
    log "[promote] 发生失败，正在从备份回滚 app/ ..."
    rm -rf "${APP_DIR}"
    tar -xzf "${BACKUP_FILE}" -C "${ROOT_DIR}"
    log "[promote] 回滚完成，app/ 已恢复到晋升前状态"
}

log "[promote] 同步已绑定摘要的 app-tmp/ -> app/"
if ! rsync -a --delete --exclude='__pycache__' --exclude='*.pyc' "${APP_TMP}/" "${APP_DIR}/"; then
    rollback
    exit 1
fi
if ! bash "${SCRIPT_DIR}/sync_fork.sh" >> "${LOG_FILE}" 2>&1; then
    log "[promote] sync_fork.sh 执行失败"
    rollback
    bash "${SCRIPT_DIR}/sync_fork.sh" >> "${LOG_FILE}" 2>&1 || true
    exit 1
fi
log "[promote] 运行时副本 app-fork/ 已刷新"

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    if (cd "${ROOT_DIR}" && docker compose restart); then
        log "[promote] 容器已重启，新代码生效"
    else
        log "[promote] 警告：docker compose restart 失败，请人工检查"
    fi
else
    log "[promote] 当前环境 docker 不可用，跳过容器重启"
fi

EVIDENCE_DIR="${ROOT_DIR}/data/promotion-history/${REQ_ID}"
mkdir -p "$(dirname "${EVIDENCE_DIR}")"
rm -rf "${EVIDENCE_DIR}"
cp -a "${REQ_DIR}" "${EVIDENCE_DIR}"
printf '%s\n' "${CURRENT_SHA}" > "${EVIDENCE_DIR}/promoted.sha256"
printf '%s\n' "$(date --iso-8601=seconds)" > "${EVIDENCE_DIR}/promoted_at"
log "[promote] 已保存晋升证据：${EVIDENCE_DIR}"

log "[promote] ===== 晋升完成：${REQ_ID}（备份：${BACKUP_FILE}）====="
rm -rf "${REQ_DIR}"
if [[ -f "${ROOT_DIR}/.env" ]] && grep -qE '^\s*GITHUB_AUTO_BACKUP\s*=\s*1\s*$' "${ROOT_DIR}/.env"; then
    if [[ -f "${SCRIPT_DIR}/github_backup.sh" ]]; then
        if bash "${SCRIPT_DIR}/github_backup.sh" "auto: 晋升 ${REQ_ID} 后的状态备份"; then
            log "[promote] GitHub 状态备份完成"
        else
            log "[promote] 警告：GitHub 状态备份失败"
        fi
    fi
fi
exit 0
