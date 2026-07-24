#!/usr/bin/env bash
# gate_check.sh — host-controlled safety gate for self-improvement candidates.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
APP_TMP="${ROOT_DIR}/app-tmp"
APP_FORK="${ROOT_DIR}/app-fork"
LOG_FILE="${ROOT_DIR}/logs/evolution.log"
TREE_DIGEST="${SCRIPT_DIR}/tree_digest.py"
REQ_ID="${1:-req-$(date +%Y%m%d-%H%M%S)}"

if [[ ! "${REQ_ID}" =~ ^[a-zA-Z0-9._-]+$ ]]; then
    echo "[gate] 错误：请求ID含有非法字符：${REQ_ID}" >&2
    exit 1
fi
REQ_DIR="${ROOT_DIR}/data/promote-requests/${REQ_ID}"
REPORT="${REQ_DIR}/report.txt"
mkdir -p "${REQ_DIR}" "${ROOT_DIR}/logs"
: > "${REPORT}"
rm -f "${REQ_DIR}/READY" "${REQ_DIR}/REJECTED" "${REQ_DIR}/candidate.sha256"

log() {
    local m="[$(date '+%F %T')] $*"
    echo "${m}"
    echo "${m}" >> "${REPORT}"
    echo "${m}" >> "${LOG_FILE}"
}
pass() { log "[PASS] $*"; }
fail() {
    log "[FAIL] $*"
    echo "拒绝原因：$*" > "${REQ_DIR}/REJECTED"
    log "[gate] 检查未通过，已写入 ${REQ_DIR}/REJECTED"
    exit 1
}

log "[gate] ===== 开始底线检查，请求ID：${REQ_ID} ====="
if [[ ! -d "${APP_TMP}" ]] || [[ -z "$(ls -A "${APP_TMP}" 2>/dev/null)" ]]; then
    fail "app-tmp/ 为空，没有可检查的改动"
fi
if [[ ! -d "${APP_FORK}" ]] || [[ -z "$(ls -A "${APP_FORK}" 2>/dev/null)" ]]; then
    fail "app-fork/ 为空，无法对比基线，请先运行 scripts/sync_fork.sh"
fi
if [[ ! -f "${TREE_DIGEST}" ]]; then
    fail "可信摘要脚本不存在：${TREE_DIGEST}"
fi

PATTERN_FILE="$(mktemp)"
PROTECTED_PATTERNS="$(mktemp)"
ADDED_LINES="$(mktemp)"
TEST_LOG=""
trap 'rm -f "${PATTERN_FILE}" "${PROTECTED_PATTERNS}" "${ADDED_LINES}" ${TEST_LOG:+"${TEST_LOG}"}' EXIT

diff -ruN --exclude='__pycache__' "${APP_FORK}" "${APP_TMP}" 2>/dev/null \
    | grep -E '^\+' | grep -vE '^\+\+\+' | sed 's/^+//' > "${ADDED_LINES}" || true

log "[gate] 检查 a/6：新增代码危险模式"
cat > "${PATTERN_FILE}" <<'PATTERNS_EOF'
rm[[:space:]]+-rf[[:space:]]+/([[:space:]"';|&)]|$)
mkfs
:\(\)[[:space:]]*\{[[:space:]]*:\|:[[:space:]]*&[[:space:]]*\}[[:space:]]*;[[:space:]]*:
(>{1,2}|tee[[:space:]]+)[[:space:]]*/etc/passwd
open\([^)]*/etc/passwd[^)]*['"][wa]
docker\.sock
curl[^|]*\|[[:space:]]*(sudo[[:space:]]+)?(ba)?sh([[:space:]]|$)
sk-[a-zA-Z0-9]{20,}
PATTERNS_EOF
if HITS="$(grep -EnI -f "${PATTERN_FILE}" "${ADDED_LINES}" 2>/dev/null)"; then
    log "命中危险模式："
    echo "${HITS}" | while IFS= read -r line; do log "  ${line}"; done
    fail "新增代码命中危险模式，拒绝晋升"
fi
pass "新增代码未发现危险模式"

log "[gate] 检查 b/6：受保护宿主机与主人配置写入意图"
cat > "${PROTECTED_PATTERNS}" <<'PROTECTED_EOF'
open\([^)]*(scripts/|\.env([^a-zA-Z]|$)|docker-compose|local/(profile|preferences|servers)|local/secrets)[^)]*['"][wax]
(shutil\.(copy|copyfile|copytree|move)|os\.(remove|rename|replace|unlink|chmod|rmdir))\([^)]*(scripts/|\.env|docker-compose|local/(profile|preferences|servers)|local/secrets)
(scripts/|\.env|docker-compose|local/(profile|preferences|servers)|local/secrets)[^#\n]*\.(write_text|write_bytes|unlink|rename|chmod)\(
(^|[^>])>>?[[:space:]]*[^[:space:]]*(scripts/|\.env|docker-compose|local/(profile|preferences|servers)|local/secrets)
\b(rm|mv|cp|tee)[[:space:]]+[^#\n|]*(scripts/|\.env|docker-compose|local/(profile|preferences|servers)|local/secrets)
PROTECTED_EOF
PROTECTED_HITS="$(grep -EnI -f "${PROTECTED_PATTERNS}" "${ADDED_LINES}" 2>/dev/null || true)"
if [[ -n "${PROTECTED_HITS}" ]]; then
    log "发现对受保护路径的写入意图："
    echo "${PROTECTED_HITS}" | while IFS= read -r line; do log "  ${line}"; done
    fail "候选代码含对宿主机控制面或主人只读配置的写入意图"
fi
pass "受保护路径检查通过"

log "[gate] 检查 c/6：安全关键应用模块不可由 Agent 自主修改"
PROTECTED_APP_FILES=(
    "core/autonomy.py"
    "core/operations.py"
    "core/permissions.py"
    "core/configuration.py"
    "core/local_context.py"
    "core/privacy.py"
    "core/memory.py"
    "skills/evolution_ops.py"
    "skills/server_ops.py"
    "skills/local_context.py"
)
for rel in "${PROTECTED_APP_FILES[@]}"; do
    baseline="${APP_FORK}/${rel}"
    candidate="${APP_TMP}/${rel}"
    if [[ -e "${baseline}" || -e "${candidate}" ]]; then
        if [[ ! -e "${baseline}" || ! -e "${candidate}" ]] || ! cmp -s "${baseline}" "${candidate}"; then
            fail "安全关键模块发生变化：${rel}；只能通过人类主导的仓库变更修改"
        fi
    fi
done
pass "安全关键模块与基线一致"

log "[gate] 检查 d/6：改动规模限值（文件数 <= 10，总行数 <= 500）"
MAX_FILES=10
MAX_LINES=500
CHANGED_FILES="$(diff -rq --exclude='__pycache__' "${APP_FORK}" "${APP_TMP}" | wc -l | tr -d ' ' || true)"
CHANGED_LINES="$(diff -ruN --exclude='__pycache__' "${APP_FORK}" "${APP_TMP}" | grep -E '^[+-]' | grep -cvE '^(\+\+\+|---)' || true)"
CHANGED_LINES="${CHANGED_LINES//[^0-9]/}"
: "${CHANGED_LINES:=0}"
log "变更文件数：${CHANGED_FILES}（上限 ${MAX_FILES}），变更行数：${CHANGED_LINES}（上限 ${MAX_LINES}）"
if (( CHANGED_FILES > MAX_FILES )); then fail "变更文件数 ${CHANGED_FILES} 超过上限 ${MAX_FILES}"; fi
if (( CHANGED_LINES > MAX_LINES )); then fail "变更行数 ${CHANGED_LINES} 超过上限 ${MAX_LINES}"; fi
pass "改动规模在限值内"

log "[gate] 检查 e/6：运行全部单元测试"
if [[ ! -d "${APP_TMP}/tests" ]]; then fail "app-tmp/ 中缺少 tests/ 目录，无法验证正确性"; fi
TEST_LOG="$(mktemp)"
if (cd "${APP_TMP}" && python3 -m pytest --version >/dev/null 2>&1); then
    TEST_CMD="python3 -m pytest tests -q"
else
    TEST_CMD='for t in tests/test_*.py; do python3 "${t}" || exit 1; done'
fi
log "测试命令：${TEST_CMD}"
if ! (cd "${APP_TMP}" && eval "${TEST_CMD}") > "${TEST_LOG}" 2>&1; then
    log "测试输出（末尾 50 行）："
    tail -n 50 "${TEST_LOG}" | while IFS= read -r line; do log "  ${line}"; done
    fail "单元测试未通过，拒绝晋升"
fi
TEST_SUMMARY="$(tail -n 3 "${TEST_LOG}" | tr '\n' ' ')"
pass "单元测试全部通过（${TEST_SUMMARY}）"

log "[gate] 检查 f/6：生成候选代码树完整性摘要"
if ! CANDIDATE_SHA="$(python3 "${TREE_DIGEST}" "${APP_TMP}")"; then fail "无法计算候选代码树摘要"; fi
if [[ ! "${CANDIDATE_SHA}" =~ ^[0-9a-f]{64}$ ]]; then fail "候选摘要格式异常：${CANDIDATE_SHA}"; fi
printf '%s\n' "${CANDIDATE_SHA}" > "${REQ_DIR}/candidate.sha256"
pass "候选摘要：${CANDIDATE_SHA}"
printf '全部底线检查通过。候选摘要：%s。可执行 scripts/promote.sh %s\n' "${CANDIDATE_SHA}" "${REQ_ID}" > "${REQ_DIR}/READY"
log "[gate] ===== 全部检查通过，READY 已绑定候选摘要 ====="
exit 0
