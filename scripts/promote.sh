#!/usr/bin/env bash
# promote.sh — promote an exact, gate-approved candidate app into app/.
#
# 信任模型：gate_check.sh 的产物写入 agent 可写的暂存队列（默认
# app-tmp/promote-requests），READY/report/摘要都可能被模型伪造。因此本脚本在
# 宿主机侧强制执行复核（re-gate，默认开启，AGENELF_PROMOTE_SKIP_REGATE=1 可跳过）：
#   1. cp -a 冻结候选快照，之后的摘要与同步全部基于快照（消除 TOCTOU）；
#   2. 用可信 tree_digest.py 重算快照摘要，与请求附带的 candidate.sha256 比对；
#   3. docker 可用时，用项目镜像以 --network none --read-only 隔离重跑候选测试；
# 全部通过后才 rsync 快照到 app/。没有真实通过的测试与匹配摘要就无法晋升。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
APP_DIR="${ROOT_DIR}/app"
APP_TMP="${ROOT_DIR}/app-tmp"
if [[ -d "${APP_TMP}/repo/app" ]]; then
    CANDIDATE_APP="${APP_TMP}/repo/app"
else
    CANDIDATE_APP="${APP_TMP}"
fi
LOG_FILE="${ROOT_DIR}/logs/evolution.log"
BACKUP_DIR="${ROOT_DIR}/data/app-backups"
TREE_DIGEST="${SCRIPT_DIR}/tree_digest.py"
TEST_RUNNER="${SCRIPT_DIR}/run_candidate_tests.py"

REQ_ID="${1:-}"
if [[ -z "${REQ_ID}" ]]; then
    echo "[promote] 错误：缺少请求ID。用法：bash scripts/promote.sh <请求ID>" >&2
    exit 1
fi
if [[ ! "${REQ_ID}" =~ ^[a-zA-Z0-9._-]+$ ]]; then
    echo "[promote] 错误：请求ID含有非法字符：${REQ_ID}" >&2
    exit 1
fi

# 请求目录解析：显式 PROMOTE_REQUESTS_DIR 优先；否则先看暂存队列（人工直接
# promote gate 刚产生的请求），再看 watcher 已导入的可信队列。
if [[ -n "${PROMOTE_REQUESTS_DIR:-}" ]]; then
    REQ_DIR="${PROMOTE_REQUESTS_DIR}/${REQ_ID}"
elif [[ -d "${APP_TMP}/promote-requests/${REQ_ID}" ]]; then
    REQ_DIR="${APP_TMP}/promote-requests/${REQ_ID}"
else
    REQ_DIR="${ROOT_DIR}/data/promote-requests/${REQ_ID}"
fi
mkdir -p "${ROOT_DIR}/logs" "${BACKUP_DIR}"

# 并发保护：同一时刻只允许一个 promote 流程，flock 不可用时退化为单流程假设。
LOCK_FILE="${ROOT_DIR}/data/promote.lock"
exec 9>"${LOCK_FILE}"
if command -v flock >/dev/null 2>&1; then
    if ! flock -n 9; then
        echo "[promote] 错误：另一个 promote.sh 正在执行，拒绝并发晋升" >&2
        exit 1
    fi
fi

log() {
    local m
    m="[$(date '+%F %T')] $*"
    echo "${m}"
    echo "${m}" >> "${LOG_FILE}"
}

log "[promote] ===== 开始晋升，请求ID：${REQ_ID} ====="
log "[promote] 候选 app：${CANDIDATE_APP}"
log "[promote] 请求目录：${REQ_DIR}"
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
if [[ ! -d "${CANDIDATE_APP}" ]] || [[ -z "$(ls -A "${CANDIDATE_APP}" 2>/dev/null)" ]]; then
    log "[promote] 错误：候选 app 不存在或为空"
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

# 冻结候选快照：摘要计算、复核测试与最终同步全部针对同一棵不可变树。
SNAPSHOT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/agenelf-promote-snapshot.XXXXXX")"
cleanup() {
    rm -rf "${SNAPSHOT_DIR}"
}
trap cleanup EXIT
cp -a "${CANDIDATE_APP}/." "${SNAPSHOT_DIR}/"
log "[promote] 已冻结候选快照：${SNAPSHOT_DIR}"

CURRENT_SHA="$(python3 "${TREE_DIGEST}" "${SNAPSHOT_DIR}")"
if [[ "${CURRENT_SHA}" != "${EXPECTED_SHA}" ]]; then
    log "[promote] 错误：候选代码在 gate 通过后发生变化"
    log "[promote] gate=${EXPECTED_SHA} current=${CURRENT_SHA}；拒绝时间差晋升"
    printf '拒绝原因：候选代码摘要变化，必须重新运行 gate_check.sh\n' > "${REQ_DIR}/REJECTED"
    rm -f "${REQ_DIR}/READY"
    exit 1
fi
log "[promote] READY、报告与候选摘要全部校验通过：${CURRENT_SHA}"

# 宿主机复核：在项目镜像内隔离重跑候选测试，伪造 READY/报告无法通过本步。
regate_with_docker() {
    command -v docker >/dev/null 2>&1 || return 2
    docker compose version >/dev/null 2>&1 || return 2
    [[ -f "${ROOT_DIR}/docker-compose.yml" ]] || return 2
    local image
    image="$(cd "${ROOT_DIR}" && docker compose images -q agenelf 2>/dev/null | head -n 1 || true)"
    if [[ -z "${image}" ]]; then
        log "[promote] 未找到项目镜像，执行 docker compose build agenelf 用于复核"
        (cd "${ROOT_DIR}" && docker compose build agenelf >> "${LOG_FILE}" 2>&1) || return 1
        image="$(cd "${ROOT_DIR}" && docker compose images -q agenelf 2>/dev/null | head -n 1 || true)"
    fi
    [[ -n "${image}" ]] || return 1
    local baseline="${APP_DIR}"
    if [[ ! -d "${baseline}/tests" ]] && [[ -d "${ROOT_DIR}/app-fork/tests" ]]; then
        baseline="${ROOT_DIR}/app-fork"
    fi
    log "[promote] 复核镜像：${image}（--network none --read-only），基线：${baseline}"
    docker run --rm --network none --read-only --tmpfs /tmp \
        -v "${SNAPSHOT_DIR}:/candidate:ro" \
        -v "${baseline}:/baseline:ro" \
        -v "${SCRIPT_DIR}:/scripts:ro" \
        "${image}" \
        sh -c 'cp -a /candidate /tmp/candidate && python3 /scripts/run_candidate_tests.py --baseline /baseline --candidate /tmp/candidate --phase candidate --timeout 600' \
        >> "${LOG_FILE}" 2>&1
}

if [[ "${AGENELF_PROMOTE_SKIP_REGATE:-0}" == "1" ]]; then
    log "[promote] AGENELF_PROMOTE_SKIP_REGATE=1，跳过宿主机复核（仅建议离线应急使用）"
else
    # 第一步：对冻结快照重跑 gate 的静态检查（危险模式、受保护文件、测试篡改、
    # 规模限值）。模型即使在 gate 通过后篡改候选并重算摘要，也无法通过本步。
    REGATE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/agenelf-promote-regate.XXXXXX")"
    log "[promote] 宿主机复核：对快照重跑 gate 静态检查"
    if ! AGENELF_GATE_BASE_APP="${APP_DIR}" \
         AGENELF_GATE_CANDIDATE_APP="${SNAPSHOT_DIR}" \
         AGENELF_GATE_CANDIDATE_REPO="${ROOT_DIR}" \
         AGENELF_GATE_SKIP_TESTS=1 \
         PROMOTE_REQUESTS_DIR="${REGATE_DIR}" \
         bash "${SCRIPT_DIR}/gate_check.sh" "${REQ_ID}" >> "${LOG_FILE}" 2>&1; then
        rm -rf "${REGATE_DIR}"
        log "[promote] 错误：快照未通过宿主机静态复核，中止晋升"
        printf '拒绝原因：宿主机静态复核未通过，请检查 logs/evolution.log 后重新运行 gate_check.sh\n' > "${REQ_DIR}/REJECTED"
        rm -f "${REQ_DIR}/READY"
        exit 1
    fi
    rm -rf "${REGATE_DIR}"
    log "[promote] 宿主机静态复核通过"
    regate_rc=0
    regate_with_docker || regate_rc=$?
    case "${regate_rc}" in
        0)
            log "[promote] 宿主机复核通过：候选测试在隔离容器中全部通过"
            ;;
        2)
            log "[promote] 警告：docker 不可用，跳过隔离复核测试（摘要校验已强制完成）"
            ;;
        *)
            log "[promote] 错误：宿主机复核失败（镜像构建或隔离测试未通过），中止晋升"
            printf '拒绝原因：宿主机复核未通过，请检查 logs/evolution.log 后重新运行 gate_check.sh\n' > "${REQ_DIR}/REJECTED"
            rm -f "${REQ_DIR}/READY"
            exit 1
            ;;
    esac
fi

TS="$(date +%Y%m%d-%H%M%S)"
BACKUP_FILE="${BACKUP_DIR}/${TS}.tar.gz"
tar_rc=0
tar -czf "${BACKUP_FILE}" --exclude='__pycache__' --exclude='*.pyc' \
    -C "${ROOT_DIR}" app || tar_rc=$?
# 可移植性：GNU tar 在文件变化时返回 1，BSD tar 无 --warning 选项；仅 >1 视为失败。
if [[ ${tar_rc} -gt 1 ]]; then
    log "[promote] 错误：备份 app/ 失败（tar 退出码 ${tar_rc}），中止晋升"
    exit 1
fi
log "[promote] 已备份 app/ -> ${BACKUP_FILE}"
rollback() {
    log "[promote] 发生失败，正在从备份回滚 app/ ..."
    rm -rf "${APP_DIR}"
    tar -xzf "${BACKUP_FILE}" -C "${ROOT_DIR}"
    log "[promote] 回滚完成，app/ 已恢复到晋升前状态"
}

log "[promote] 同步已绑定摘要的候选快照 -> app/"
if command -v rsync >/dev/null 2>&1; then
    # --checksum：晋升已与树摘要绑定，不能依赖 size+mtime 快速检查而漏拷
    if ! rsync -a --delete --checksum --exclude='__pycache__' --exclude='*.pyc' --exclude='/promote-requests/' \
        "${SNAPSHOT_DIR}/" "${APP_DIR}/"; then
        rollback
        exit 1
    fi
else
    log "[promote] 未找到 rsync，使用 tar 镜像兜底"
    if ! (find "${APP_DIR}" -mindepth 1 -delete && \
        (cd "${SNAPSHOT_DIR}" && tar cf - --exclude='__pycache__' --exclude='*.pyc' --exclude='./promote-requests' .) \
        | (cd "${APP_DIR}" && tar xf -)); then
        rollback
        exit 1
    fi
fi
if ! bash "${SCRIPT_DIR}/sync_fork.sh" >> "${LOG_FILE}" 2>&1; then
    log "[promote] sync_fork.sh 执行失败"
    rollback
    bash "${SCRIPT_DIR}/sync_fork.sh" >> "${LOG_FILE}" 2>&1 || true
    exit 1
fi
log "[promote] 运行时兼容副本 app-fork/ 已刷新"

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
printf '%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "${EVIDENCE_DIR}/promoted_at"
printf '%s\n' "${CANDIDATE_APP}" > "${EVIDENCE_DIR}/candidate-app-path.txt"
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
