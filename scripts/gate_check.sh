#!/usr/bin/env bash
# gate_check.sh — host-controlled safety gate for ordinary self-improvement candidates.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
APP_TMP="${ROOT_DIR}/app-tmp"
# 基线与候选可用环境变量覆盖：promote.sh 的宿主机复核会对冻结快照重跑静态检查。
BASE_APP="${AGENELF_GATE_BASE_APP:-${ROOT_DIR}/app}"
[[ -d "${BASE_APP}" ]] || BASE_APP="${ROOT_DIR}/app-fork"
if [[ -n "${AGENELF_GATE_CANDIDATE_APP:-}" ]]; then
    CANDIDATE_APP="${AGENELF_GATE_CANDIDATE_APP}"
    CANDIDATE_REPO="${AGENELF_GATE_CANDIDATE_REPO:-$(dirname "${CANDIDATE_APP}")}"
elif [[ -d "${APP_TMP}/repo/app" ]]; then
    CANDIDATE_APP="${APP_TMP}/repo/app"
    CANDIDATE_REPO="${APP_TMP}/repo"
else
    CANDIDATE_APP="${APP_TMP}"
    CANDIDATE_REPO="${ROOT_DIR}"
fi
LOG_FILE="${ROOT_DIR}/logs/evolution.log"
TREE_DIGEST="${SCRIPT_DIR}/tree_digest.py"
TEST_RUNNER="${SCRIPT_DIR}/run_candidate_tests.py"
REQ_ID="${1:-req-$(date +%Y%m%d-%H%M%S)}"

if [[ ! "${REQ_ID}" =~ ^[a-zA-Z0-9._-]+$ ]]; then
    echo "[gate] 错误：请求ID含有非法字符：${REQ_ID}" >&2
    exit 1
fi
# gate 产物写入 agent 可触发的暂存队列（默认 app-tmp/promote-requests，可用
# PROMOTE_REQUESTS_DIR 覆盖）；宿主机 watcher 复核后才移入可信的
# data/promote-requests，promote.sh 晋升前还会重新校验摘要并重跑测试。
PROMOTE_REQUESTS_DIR="${PROMOTE_REQUESTS_DIR:-${ROOT_DIR}/app-tmp/promote-requests}"
REQ_DIR="${PROMOTE_REQUESTS_DIR}/${REQ_ID}"
REPORT="${REQ_DIR}/report.txt"
mkdir -p "${REQ_DIR}" "${ROOT_DIR}/logs"
: > "${REPORT}"
rm -f "${REQ_DIR}/READY" "${REQ_DIR}/REJECTED" "${REQ_DIR}/candidate.sha256"

log() {
    local m
    m="[$(date '+%F %T')] $*"
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
log "[gate] 基线 app：${BASE_APP}"
log "[gate] 候选 app：${CANDIDATE_APP}（repo=${CANDIDATE_REPO}）"
if [[ ! -d "${CANDIDATE_APP}" ]] || [[ -z "$(ls -A "${CANDIDATE_APP}" 2>/dev/null)" ]]; then
    fail "候选 app 为空，没有可检查的改动"
fi
if [[ ! -d "${BASE_APP}" ]] || [[ -z "$(ls -A "${BASE_APP}" 2>/dev/null)" ]]; then
    fail "当前 app 基线为空，无法对比"
fi
if [[ ! -f "${TREE_DIGEST}" ]]; then
    fail "可信摘要脚本不存在：${TREE_DIGEST}"
fi

PATTERN_FILE="$(mktemp)"
PROTECTED_PATTERNS="$(mktemp)"
ADDED_LINES="$(mktemp)"
TEST_LOG=""
trap 'rm -f "${PATTERN_FILE}" "${PROTECTED_PATTERNS}" "${ADDED_LINES}" ${TEST_LOG:+"${TEST_LOG}"}' EXIT

diff -ruN --exclude='__pycache__' --exclude='*.pyc' "${BASE_APP}" "${CANDIDATE_APP}" 2>/dev/null \
    | grep -E '^\+' | grep -vE '^\+\+\+' | sed 's/^+//' > "${ADDED_LINES}" || true

log "[gate] 检查 a/7：新增代码危险模式"
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

log "[gate] 检查 b/7：受保护宿主机与主人配置写入意图"
cat > "${PROTECTED_PATTERNS}" <<'PROTECTED_EOF'
open\([^)]*(scripts/|\.env([^a-zA-Z]|$)|docker-compose|local/(profile|preferences|servers|memory|self)|local/(validation|models|repositories)|local/secrets|code-workspaces|repair-space)[^)]*['"][wax]
(shutil\.(copy|copyfile|copytree|move)|os\.(remove|rename|replace|unlink|chmod|rmdir))\([^)]*(scripts/|\.env|docker-compose|local/(profile|preferences|servers|memory|self)|local/(validation|models|repositories)|local/secrets|code-workspaces|repair-space)
(scripts/|\.env|docker-compose|local/(profile|preferences|servers|memory|self)|local/(validation|models|repositories)|local/secrets|code-workspaces|repair-space)[^#\n]*\.(write_text|write_bytes|unlink|rename|chmod)\(
(^|[^>])>>?[[:space:]]*[^[:space:]]*(scripts/|\.env|docker-compose|local/(profile|preferences|servers|memory|self)|local/(validation|models|repositories)|local/secrets|code-workspaces|repair-space)
\b(rm|mv|cp|tee)[[:space:]]+[^#\n|]*(scripts/|\.env|docker-compose|local/(profile|preferences|servers|memory|self)|local/(validation|models|repositories)|local/secrets|code-workspaces|repair-space)
PROTECTED_EOF
PROTECTED_HITS="$(grep -EnI -f "${PROTECTED_PATTERNS}" "${ADDED_LINES}" 2>/dev/null || true)"
if [[ -n "${PROTECTED_HITS}" ]]; then
    log "发现对受保护路径的写入意图："
    echo "${PROTECTED_HITS}" | while IFS= read -r line; do log "  ${line}"; done
    fail "候选代码含对宿主机控制面或主人持久化数据的写入意图"
fi
pass "受保护路径检查通过"

log "[gate] 检查 c/7：安全关键应用模块不可由普通沙盒修改"
PROTECTED_APP_FILES=(
    "api.py"
    "cli.py"
    "core/agent.py"
    "core/llm.py"
    "core/interactive_prompt.py"
    "core/context.py"
    "core/autonomy.py"
    "core/operations.py"
    "core/operation_revocation.py"
    "core/permissions.py"
    "core/configuration.py"
    "core/local_context.py"
    "core/privacy.py"
    "core/memory.py"
    "core/self_development.py"
    "core/validation.py"
    "core/capability_health.py"
    "core/registry.py"
    "core/policy.py"
    "core/execution_policy.py"
    "core/capabilities.py"
    "core/code_repair.py"
    "core/task_engine.py"
    "core/model_router.py"
    "core/channel_envelope.py"
    "core/self_optimization.py"
    "core/continuous_chat.py"
    "core/reasoning_trace.py"
    "core/evolution_workspace.py"
    "core/authorized_upgrade.py"
    "core/upgrade_redlines.py"
    "core/approval_catalog.py"
    "core/owner_approval.py"
    "core/cli_approval.py"
    "core/runtime_health.py"
    "skills/authorized_self_upgrade.py"
    "skills/authorized_upgrade_redlines.py"
    "skills/runtime_doctor.py"
    "skills/operation_control.py"
    "skills/code_repair.py"
    "skills/code_writer.py"
    "skills/skill_forge.py"
    "skills/workflow_tasks.py"
    "skills/model_routing.py"
    "skills/evolution_ops.py"
    "skills/evolution_scope_guard.py"
    "skills/server_ops.py"
    "skills/compose_lifecycle.py"
    "skills/docker_ops.py"
    "skills/authorized_upgrade_recovery.py"
    "skills/self_optimize.py"
    "skills/zz_transport_resilience.py"
    "skills/local_context.py"
    "skills/self_development.py"
    "skills/software_validation.py"
)
for rel in "${PROTECTED_APP_FILES[@]}"; do
    baseline="${BASE_APP}/${rel}"
    candidate="${CANDIDATE_APP}/${rel}"
    if [[ -e "${baseline}" || -e "${candidate}" ]]; then
        if [[ ! -e "${baseline}" || ! -e "${candidate}" ]] || ! cmp -s "${baseline}" "${candidate}"; then
            fail "安全关键模块发生变化：${rel}；普通沙盒不可修改，请使用主人两阶段授权升级"
        fi
    fi
done
pass "授权升级、请求撤销、Runner 心跳与其他安全关键模块保持基线一致"

log "[gate] 检查 d/7：既有测试和测试夹具不可删除或修改"
if [[ ! -d "${BASE_APP}/tests" ]] || [[ ! -d "${CANDIDATE_APP}/tests" ]]; then
    fail "基线或候选缺少 tests/ 目录"
fi
while IFS= read -r -d '' baseline_test; do
    rel="${baseline_test#${BASE_APP}/}"
    candidate_test="${CANDIDATE_APP}/${rel}"
    if [[ ! -f "${candidate_test}" ]]; then
        fail "既有测试被删除：${rel}"
    fi
    if ! cmp -s "${baseline_test}" "${candidate_test}"; then
        fail "既有测试被修改：${rel}；禁止削弱测试或 monkey-patch 门禁"
    fi
done < <(find "${BASE_APP}/tests" -type f ! -path '*/__pycache__/*' ! -name '*.pyc' -print0)
pass "既有测试全部保持字节级一致；候选只能新增 test_*.py"

log "[gate] 检查 e/7：改动规模限值（文件数 <= 10，总行数 <= 500）"
MAX_FILES=10
MAX_LINES=500
CHANGED_FILES="$(diff -rq --exclude='__pycache__' --exclude='*.pyc' "${BASE_APP}" "${CANDIDATE_APP}" | wc -l | tr -d ' ' || true)"
CHANGED_LINES="$(diff -ruN --exclude='__pycache__' --exclude='*.pyc' "${BASE_APP}" "${CANDIDATE_APP}" | grep -E '^[+-]' | grep -cvE '^(\+\+\+|---)' || true)"
CHANGED_LINES="${CHANGED_LINES//[^0-9]/}"
: "${CHANGED_LINES:=0}"
log "变更文件数：${CHANGED_FILES}（上限 ${MAX_FILES}），变更行数：${CHANGED_LINES}（上限 ${MAX_LINES}）"
if (( CHANGED_FILES > MAX_FILES )); then fail "变更文件数 ${CHANGED_FILES} 超过上限 ${MAX_FILES}"; fi
if (( CHANGED_LINES > MAX_LINES )); then fail "变更行数 ${CHANGED_LINES} 超过上限 ${MAX_LINES}"; fi
pass "改动规模在限值内"

log "[gate] 检查 f/7：可信基线与候选新增测试分离执行"
TEST_LOG="$(mktemp)"
if [[ "${AGENELF_GATE_SKIP_TESTS:-0}" == "1" ]]; then
    log "AGENELF_GATE_SKIP_TESTS=1，测试阶段由调用方（如 promote 隔离复核）负责"
elif [[ -f "${TEST_RUNNER}" ]]; then
    TEST_CMD=(python3 "${TEST_RUNNER}" --baseline "${BASE_APP}" --candidate "${CANDIDATE_APP}" --phase candidate --timeout 300)
    log "测试命令：${TEST_CMD[*]}"
    if ! "${TEST_CMD[@]}" > "${TEST_LOG}" 2>&1; then
        log "测试输出（末尾 80 行）："
        tail -n 80 "${TEST_LOG}" | while IFS= read -r line; do log "  ${line}"; done
        fail "可信基线或候选新增测试未通过，拒绝晋升"
    fi
else
    log "兼容模式：run_candidate_tests.py 不存在，执行 legacy unittest discover"
    if ! (cd "${CANDIDATE_APP}" && export PYTHONPATH="${CANDIDATE_APP}:${CANDIDATE_REPO}${PYTHONPATH:+:${PYTHONPATH}}" && python3 -m unittest discover -s tests -v) > "${TEST_LOG}" 2>&1; then
        tail -n 80 "${TEST_LOG}" | while IFS= read -r line; do log "  ${line}"; done
        fail "单元测试未通过，拒绝晋升"
    fi
fi
if [[ "${AGENELF_GATE_SKIP_TESTS:-0}" == "1" ]]; then
    pass "静态检查完成（测试阶段已按调用方要求跳过）"
else
    TEST_SUMMARY="$(tail -n 5 "${TEST_LOG}" | tr '\n' ' ')"
    pass "测试全部通过（${TEST_SUMMARY}）"
fi

log "[gate] 检查 g/7：生成候选 app 树完整性摘要"
if ! CANDIDATE_SHA="$(python3 "${TREE_DIGEST}" "${CANDIDATE_APP}")"; then fail "无法计算候选代码树摘要"; fi
if [[ ! "${CANDIDATE_SHA}" =~ ^[0-9a-f]{64}$ ]]; then fail "候选摘要格式异常：${CANDIDATE_SHA}"; fi
printf '%s\n' "${CANDIDATE_SHA}" > "${REQ_DIR}/candidate.sha256"
printf '%s\n' "${CANDIDATE_APP}" > "${REQ_DIR}/candidate-app-path.txt"
pass "候选摘要：${CANDIDATE_SHA}"
printf '全部底线检查通过。候选摘要：%s。可执行 scripts/promote.sh %s\n' "${CANDIDATE_SHA}" "${REQ_ID}" > "${REQ_DIR}/READY"
log "[gate] ===== 全部检查通过，READY 已绑定候选摘要 ====="
exit 0
