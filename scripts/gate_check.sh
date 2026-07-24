#!/usr/bin/env bash
# gate_check.sh — 自我迭代底线检查门（agent 在容器内只能触发，不能修改）
#
# 对 app-tmp/ 中的候选改动依次执行：
#   a. 安全底线扫描（危险模式，命中即拒绝）
#   b. 受保护路径检查（不得含对 scripts/、.env、docker-compose.yml 的写入意图）
#   c. 改动规模限值（与 app-fork 对比：变更文件数 <= 10，总变更行数 <= 500）
#   d. 完整测试（pytest 可用则优先，否则逐个运行 tests/test_*.py）
# 全部通过：在 data/promote-requests/<请求ID>/ 写 report.txt 与 READY 标记；
# 任一失败：写 REJECTED 与原因，退出码 1。全程日志追加 logs/evolution.log。
#
# 用法：bash scripts/gate_check.sh [请求ID]   （缺省用时间戳生成）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

APP_TMP="${ROOT_DIR}/app-tmp"
APP_FORK="${ROOT_DIR}/app-fork"
LOG_FILE="${ROOT_DIR}/logs/evolution.log"
REQ_ID="${1:-req-$(date +%Y%m%d-%H%M%S)}"

# 请求ID合法性校验（防路径穿越）
if [[ ! "${REQ_ID}" =~ ^[a-zA-Z0-9._-]+$ ]]; then
    echo "[gate] 错误：请求ID含有非法字符：${REQ_ID}" >&2
    exit 1
fi

REQ_DIR="${ROOT_DIR}/data/promote-requests/${REQ_ID}"
REPORT="${REQ_DIR}/report.txt"
mkdir -p "${REQ_DIR}" "${ROOT_DIR}/logs"
: > "${REPORT}"
rm -f "${REQ_DIR}/READY" "${REQ_DIR}/REJECTED"

# 同时输出到控制台、报告文件与演进日志
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

# ---------- a. 安全底线扫描 ----------
log "[gate] 检查 a/4：安全底线扫描（危险模式）"
PATTERN_FILE="$(mktemp)"
trap 'rm -f "${PATTERN_FILE}"' EXIT
# 危险模式清单（ERE 正则，逐行一个）：
#   1) rm -rf / 根目录删除        2) mkfs 格式化磁盘
#   3) fork 炸弹 :(){ :|:& };:    4) 写入 /etc/passwd
#   5) docker.sock（容器逃逸）    6) curl ... | sh 远程脚本直执行
#   7) 硬编码密钥 sk-xxxxxxxx（20位以上）
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
if HITS="$(grep -rEnI --exclude-dir='__pycache__' -f "${PATTERN_FILE}" "${APP_TMP}" 2>/dev/null)"; then
    log "命中危险模式："
    echo "${HITS}" | while IFS= read -r line; do log "  ${line}"; done
    fail "安全底线扫描命中危险模式，拒绝晋升"
fi
pass "安全底线扫描：未发现危险模式"

# ---------- b. 受保护路径检查 ----------
log "[gate] 检查 b/4：受保护路径写入意图（scripts/、.env、docker-compose.yml）"
# 单行结构匹配「写操作 + 受保护路径」的组合，避免把文档字符串、注释、
# 以及合法的 subprocess 触发（bash scripts/gate_check.sh）误判为写入意图。
# 模式清单（ERE）：
#   1) open('...scripts/...', 'w'/'a'/'x')    以写模式打开受保护路径
#   2) shutil.copy/move、os.remove/rename/chmod 等作用于受保护路径
#   3) Path('...scripts/...').write_text()/unlink() 等 Path 写方法
#   4) shell 重定向写入受保护路径（> / >> / tee）
#   5) shell 文件命令 rm/mv/cp 作用于受保护路径
PROTECTED_PATTERNS="$(mktemp)"
cat > "${PROTECTED_PATTERNS}" <<'PROTECTED_EOF'
open\([^)]*(scripts/|\.env([^a-zA-Z]|$)|docker-compose)[^)]*['"][wax]
(shutil\.(copy|copyfile|copytree|move)|os\.(remove|rename|replace|unlink|chmod|rmdir))\([^)]*(scripts/|\.env|docker-compose)
(scripts/|\.env|docker-compose)[^#\n]*\.(write_text|write_bytes|unlink|rename|chmod)\(
(^|[^>])>>?[[:space:]]*[^[:space:]]*(scripts/|\.env|docker-compose)
\b(rm|mv|cp|tee)[[:space:]]+[^#\n|]*(scripts/|\.env|docker-compose)
PROTECTED_EOF
PROTECTED_HITS="$(grep -rEnI --exclude-dir='__pycache__' \
    -f "${PROTECTED_PATTERNS}" "${APP_TMP}" 2>/dev/null || true)"
if [[ -n "${PROTECTED_HITS}" ]]; then
    log "发现对受保护路径的写入意图："
    echo "${PROTECTED_HITS}" | while IFS= read -r line; do log "  ${line}"; done
    fail "app-tmp/ 中含对 scripts/、.env 或 docker-compose.yml 的写入意图，拒绝晋升"
fi
pass "受保护路径检查：未发现写入意图"

# ---------- c. 改动规模限值 ----------
log "[gate] 检查 c/4：改动规模限值（文件数 <= 10，总行数 <= 500）"
MAX_FILES=10
MAX_LINES=500
if [[ ! -d "${APP_FORK}" ]] || [[ -z "$(ls -A "${APP_FORK}" 2>/dev/null)" ]]; then
    fail "app-fork/ 为空，无法对比基线，请先运行 scripts/sync_fork.sh"
fi
CHANGED_FILES="$(diff -rq --exclude='__pycache__' "${APP_FORK}" "${APP_TMP}" | wc -l | tr -d ' ' || true)"
CHANGED_LINES="$(diff -ruN --exclude='__pycache__' "${APP_FORK}" "${APP_TMP}" \
    | grep -E '^[+-]' | grep -cvE '^(\+\+\+|---)' || true)"
CHANGED_LINES="${CHANGED_LINES//[^0-9]/}"
: "${CHANGED_LINES:=0}"
log "变更文件数：${CHANGED_FILES}（上限 ${MAX_FILES}），变更行数：${CHANGED_LINES}（上限 ${MAX_LINES}）"
if (( CHANGED_FILES > MAX_FILES )); then
    fail "变更文件数 ${CHANGED_FILES} 超过上限 ${MAX_FILES}，拒绝暴走式重写"
fi
if (( CHANGED_LINES > MAX_LINES )); then
    fail "变更行数 ${CHANGED_LINES} 超过上限 ${MAX_LINES}，拒绝暴走式重写"
fi
pass "改动规模：${CHANGED_FILES} 个文件 / ${CHANGED_LINES} 行，均在限值内"

# ---------- d. 完整测试 ----------
log "[gate] 检查 d/4：运行全部单元测试"
if [[ ! -d "${APP_TMP}/tests" ]]; then
    fail "app-tmp/ 中缺少 tests/ 目录，无法验证正确性"
fi
TEST_LOG="$(mktemp)"
if (cd "${APP_TMP}" && python3 -m pytest --version >/dev/null 2>&1); then
    # pytest 可用则优先
    TEST_CMD="python3 -m pytest tests -q"
else
    # 兜底：逐个运行 tests/test_*.py（每个文件内部自含 unittest main）
    TEST_CMD='for t in tests/test_*.py; do python3 "${t}" || exit 1; done'
fi
log "测试命令：${TEST_CMD}"
if ! (cd "${APP_TMP}" && eval "${TEST_CMD}") > "${TEST_LOG}" 2>&1; then
    log "测试输出（末尾 50 行）："
    tail -n 50 "${TEST_LOG}" | while IFS= read -r line; do log "  ${line}"; done
    rm -f "${TEST_LOG}"
    fail "单元测试未通过，拒绝晋升"
fi
TEST_SUMMARY="$(tail -n 3 "${TEST_LOG}" | tr '\n' ' ')"
rm -f "${TEST_LOG}"
pass "单元测试全部通过（${TEST_SUMMARY}）"

# ---------- e. 全部通过：写 READY 标记 ----------
echo "全部底线检查通过，可执行 scripts/promote.sh ${REQ_ID}" > "${REQ_DIR}/READY"
log "[gate] ===== 全部检查通过，已写入 ${REQ_DIR}/READY ====="
exit 0
