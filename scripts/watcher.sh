#!/usr/bin/env bash
# watcher.sh — host-side observer for READY evolution requests.
# Safe default: notification only. Set AGENELF_AUTO_PROMOTE_EVOLUTION=1 in .env
# only when the operator intentionally accepts automatic promotion.
#
# gate_check.sh（可由 agent 在容器内触发）把请求写入暂存队列
# ${PROMOTE_REQUESTS_DIR:-app-tmp/promote-requests}；本脚本在宿主机复核暂存产物
# 后才移入可信队列 data/promote-requests（agent 只读）。晋升本身由 promote.sh
# 执行，它会重新冻结快照、校验摘要并在 docker 可用时隔离重跑候选测试，
# 因此伪造 READY/report 无法完成晋升。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
STAGING_DIR="${PROMOTE_REQUESTS_DIR:-${ROOT_DIR}/app-tmp/promote-requests}"
REQUESTS_DIR="${ROOT_DIR}/data/promote-requests"
LOG_FILE="${ROOT_DIR}/logs/evolution.log"
INTERVAL="${AGENELF_WATCH_INTERVAL:-10}"
AUTO_PROMOTE="0"

if [[ -f "${ROOT_DIR}/.env" ]]; then
    VALUE="$(grep -E '^\s*AGENELF_AUTO_PROMOTE_EVOLUTION\s*=' "${ROOT_DIR}/.env" | tail -1 | cut -d= -f2- | tr -d '[:space:]' || true)"
    [[ "${VALUE}" == "1" ]] && AUTO_PROMOTE="1"
fi

mkdir -p "${STAGING_DIR}" "${REQUESTS_DIR}" "${ROOT_DIR}/logs"
log() {
    local m
    m="[$(date '+%F %T')] $*"
    echo "${m}"
    echo "${m}" >> "${LOG_FILE}"
}

# 宿主机侧复核暂存请求：结构不完整或明显被篡改的请求标记 REJECTED 后移出
# 暂存区，防止 agent 反复重写 READY 造成通知/晋升风暴。
validate_staging_request() {
    local req_dir="$1"
    [[ -f "${req_dir}/READY" ]] || return 1
    [[ -f "${req_dir}/report.txt" ]] || return 1
    ! grep -q '^\[.*\] \[FAIL\]' "${req_dir}/report.txt" || return 1
    [[ -f "${req_dir}/candidate.sha256" ]] || return 1
    local sha
    sha="$(tr -d '[:space:]' < "${req_dir}/candidate.sha256")"
    [[ "${sha}" =~ ^[0-9a-f]{64}$ ]] || return 1
    return 0
}

import_staging_requests() {
    local ready req_dir req_id
    shopt -s nullglob
    for ready in "${STAGING_DIR}"/*/READY; do
        req_dir="$(dirname "${ready}")"
        req_id="$(basename "${req_dir}")"
        if [[ ! "${req_id}" =~ ^[a-zA-Z0-9._-]+$ ]]; then
            log "[watcher] 暂存请求ID非法，忽略：${req_id}"
            continue
        fi
        if validate_staging_request "${req_dir}"; then
            rm -rf -- "${REQUESTS_DIR:?}/${req_id}"
            if mv "${req_dir}" "${REQUESTS_DIR}/${req_id}"; then
                log "[watcher] 暂存请求 ${req_id} 复核通过，已移入可信队列 data/promote-requests"
            else
                log "[watcher] 暂存请求 ${req_id} 移入可信队列失败"
            fi
        else
            printf '拒绝原因：暂存产物不完整或校验失败（READY/report/摘要），宿主 watcher 拒绝导入\n' \
                > "${req_dir}/REJECTED"
            rm -f "${req_dir}/READY"
            rm -rf -- "${REQUESTS_DIR:?}/${req_id}"
            if mv "${req_dir}" "${REQUESTS_DIR}/${req_id}"; then
                log "[watcher] 暂存请求 ${req_id} 未通过宿主机复核，已标记 REJECTED 并移出暂存区"
            fi
        fi
    done
    shopt -u nullglob
}

log "[watcher] 启动：auto_promote=${AUTO_PROMOTE}，间隔 ${INTERVAL}s，暂存队列 ${STAGING_DIR}"
while true; do
    import_staging_requests
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
            printf '%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "${req_dir}/NOTIFIED"
        fi
    done
    shopt -u nullglob
    sleep "${INTERVAL}"
done
