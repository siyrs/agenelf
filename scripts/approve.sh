#!/usr/bin/env bash
# approve.sh — 高危命令人类裁决闸门（人类专属，宿主机执行）
#
# ⚠️ 本脚本是"agent 提议，人类裁决"安全模型的最后一道闸门：
#   - 容器内 scripts/ 为只读挂载，agent 无法修改本脚本；
#   - agent 只能在 data/auth-requests/ 下创建 pending 请求并读取裁决结果，
#     永远不能把请求改成 approved/denied——那是人类通过本脚本才有的权力；
#   - 裁决动作本身也会写入 logs/audit.log，全程可追溯。
#
# 用法：
#   bash scripts/approve.sh <request_id> [approve|deny] [拒绝理由]
#     request_id   授权请求 ID（agent 拦截提示中给出的 auth-xxxxxxxxxxxx）
#     approve|deny 裁决动作，缺省为 approve
#     拒绝理由     仅在 deny 时可填，记入请求文件与审计日志
#
# approve 时会刷新 expires_at 为当前时间 +300 秒（一次性授权，核销后作废）。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

REQ_ID="${1:-}"
ACTION="${2:-approve}"
DENY_REASON="${3:-}"
REQ_DIR="${ROOT_DIR}/data/auth-requests"
LOG_FILE="${ROOT_DIR}/logs/audit.log"

usage() {
    echo "用法：bash scripts/approve.sh <request_id> [approve|deny] [拒绝理由]" >&2
    exit 2
}

# 参数校验
[[ -n "${REQ_ID}" ]] || usage
if [[ ! "${REQ_ID}" =~ ^[a-zA-Z0-9._-]+$ ]]; then
    echo "[approve] 错误：请求ID含有非法字符：${REQ_ID}" >&2
    exit 1
fi
if [[ "${ACTION}" != "approve" && "${ACTION}" != "deny" ]]; then
    echo "[approve] 错误：动作只能是 approve 或 deny，收到：${ACTION}" >&2
    usage
fi

REQ_FILE="${REQ_DIR}/${REQ_ID}.json"
if [[ ! -f "${REQ_FILE}" ]]; then
    echo "[approve] 错误：授权请求不存在：${REQ_FILE}" >&2
    exit 1
fi

mkdir -p "${ROOT_DIR}/logs"

# 同时输出到控制台与审计日志
log() {
    local m="[$(date '+%F %T')] $*"
    echo "${m}"
    echo "${m}" >> "${LOG_FILE}"
}

# 先读取当前状态，只允许对 pending 请求裁决
CURRENT_STATUS="$(python3 - "${REQ_FILE}" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    print(json.load(f).get("status", ""))
PY
)"
if [[ "${CURRENT_STATUS}" != "pending" ]]; then
    echo "[approve] 拒绝裁决：请求 ${REQ_ID} 当前状态为 ${CURRENT_STATUS}，仅 pending 可裁决" >&2
    exit 1
fi

# 内联 python3 更新 JSON：status / decided_at / decided_by($USER) /
# （deny 时）拒绝理由 /（approve 时）刷新 expires_at = 现在 +300s
DECIDED_BY="${USER:-unknown}" \
DENY_REASON="${DENY_REASON}" \
python3 - "${REQ_FILE}" "${ACTION}" <<'PY'
import json
import os
import sys
from datetime import datetime, timedelta

path, action = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as f:
    data = json.load(f)

now = datetime.now().astimezone()
data["status"] = "approved" if action == "approve" else "denied"
data["decided_at"] = now.isoformat(timespec="seconds")
data["decided_by"] = os.environ.get("DECIDED_BY", "unknown")
if action == "approve":
    # 批准后重新给 300 秒窗口，agent 需在窗口内核销执行
    data["expires_at"] = (now + timedelta(seconds=300)).isoformat(timespec="seconds")
else:
    reason = os.environ.get("DENY_REASON", "").strip()
    if reason:
        data["deny_reason"] = reason

with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
    f.write("\n")

print(data["status"])
PY

# 打印裁决结果并写审计日志
if [[ "${ACTION}" == "approve" ]]; then
    log "[approve] 请求 ${REQ_ID} 已批准（裁决人：${USER:-unknown}，300 秒内有效，一次性核销）"
    echo "[approve] 授权详情：$(python3 -c "import json;d=json.load(open('${REQ_FILE}',encoding='utf-8'));print(d.get('skill',''),d.get('action',''),repr(d.get('detail','')))")"
else
    if [[ -n "${DENY_REASON}" ]]; then
        log "[deny] 请求 ${REQ_ID} 已拒绝（裁决人：${USER:-unknown}，理由：${DENY_REASON}）"
    else
        log "[deny] 请求 ${REQ_ID} 已拒绝（裁决人：${USER:-unknown}）"
    fi
fi
