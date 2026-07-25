#!/usr/bin/env bash
# watcher.sh — host-side observer for READY evolution requests.
# Safe default: notification only. Set AGENELF_AUTO_PROMOTE_EVOLUTION=1 in .env
# only when the operator intentionally accepts automatic promotion.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REQUESTS_DIR="${ROOT_DIR}/data/promote-requests"
LOG_FILE="${ROOT_DIR}/logs/evolution.log"
INTERVAL="${AGENELF_WATCH_INTERVAL:-10}"
AUTO_PROMOTE="0"

if [[ -f "${ROOT_DIR}/.env" ]]; then
    VALUE="$(grep -E '^\s*AGENELF_AUTO_PROMOTE_EVOLUTION\s*=' "${ROOT_DIR}/.env" | tail -1 | cut -d= -f2- | tr -d '[:space:]' || true)"
    [[ "${VALUE}" == "1" ]] && AUTO_PROMOTE="1"
fi

mkdir -p "${REQUESTS_DIR}" "${ROOT_DIR}/logs"
log() {
    local m
    m="[$(date '+%F %T')] $*"
    echo "${m}"
    echo "${m}" >> "${LOG_FILE}"
}

log "[watcher] 启动：auto_promote=${AUTO_PROMOTE}，间隔 ${INTERVAL}s"
while true; do
    shopt -s nullglob
    for ready in "${REQUESTS_DIR}"/*/READY; do
        req_dir="$(dirname "${ready}")"
        req_id="$(basename "${req_dir}")"
        if [[ "${AUTO_PROMOTE}" == "1" ]]; then
            log "[watcher] 自动晋升已显式启用，处理 ${req_id}"
            if bash "${SCRIPT_DIR}/promote.sh" "${req_id}"; then
                log "[watcher] 请求 ${req_id} 晋升成功"
            else
                log "[watcher] 请求 ${req_id} 晋升失败"
            fi
        elif [[ ! -f "${req_dir}/NOTIFIED" ]]; then
            log "[watcher] 请求 ${req_id} 已 READY，等待人工执行：make promote REQ=${req_id}"
            printf '%s\n' "$(date --iso-8601=seconds)" > "${req_dir}/NOTIFIED"
        fi
    done
    shopt -u nullglob
    sleep "${INTERVAL}"
done
