#!/usr/bin/env bash
# approve.sh — host-only decision gate for exact Agenelf operation requests.
#
# Usage:
#   bash scripts/approve.sh <op-or-auth-id> [approve|deny] [reason] [--as <name>]
#
# The script never edits the Agent-writable request.  It creates a separate
# decision document in data/auth-decisions/.  That directory must be mounted
# read-only into the Agent container and read-only into the ops runner.
#
# 多票（双签）请求（请求 JSON 含 required_approvers>1）：
#   每次 approve 向裁决文件的 approvals 数组追加一票（decided_by 取 $USER
#   或 --as <name>）；不同批准人票数达到 required_approvers 才把 decision
#   翻为 approved 并刷新 expires_at；同一批准人重复投票退出码 3；
#   deny 任意时刻一票即决。单签请求行为与历史版本完全一致。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REQUEST_ID=""
ACTION="approve"
REASON=""
DECIDED_BY="${USER:-unknown}"
DECISIONS_DIR="${ROOT_DIR}/data/auth-decisions"
LOG_FILE="${ROOT_DIR}/logs/audit.log"

usage() {
  echo "用法：bash scripts/approve.sh <op-or-auth-id> [approve|deny] [reason] [--as <name>]" >&2
  exit 2
}

POSITIONAL=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --as)
      [[ $# -ge 2 ]] || usage
      DECIDED_BY="$2"
      shift 2
      ;;
    --as=*)
      DECIDED_BY="${1#--as=}"
      shift
      ;;
    *)
      POSITIONAL+=("$1")
      shift
      ;;
  esac
done
REQUEST_ID="${POSITIONAL[0]:-}"
ACTION="${POSITIONAL[1]:-approve}"
REASON="${POSITIONAL[2]:-}"

[[ -n "${REQUEST_ID}" ]] || usage
[[ "${REQUEST_ID}" =~ ^(op-[0-9a-f]{16}|auth-[0-9a-f]{12})$ ]] || {
  echo "[approve] 非法请求 ID：${REQUEST_ID}" >&2
  exit 1
}
[[ "${ACTION}" == "approve" || "${ACTION}" == "deny" ]] || usage
[[ "${DECIDED_BY}" =~ ^[A-Za-z0-9._@-]{1,64}$ ]] || {
  echo "[approve] 非法批准人标识：${DECIDED_BY}" >&2
  exit 2
}

if [[ "${REQUEST_ID}" == op-* ]]; then
  REQUEST_FILE="${ROOT_DIR}/data/ops-requests/${REQUEST_ID}.json"
else
  REQUEST_FILE="${ROOT_DIR}/data/auth-requests/${REQUEST_ID}.json"
fi
[[ -f "${REQUEST_FILE}" ]] || {
  echo "[approve] 请求不存在：${REQUEST_FILE}" >&2
  exit 1
}

mkdir -p "${DECISIONS_DIR}" "${ROOT_DIR}/logs"
DECISION_FILE="${DECISIONS_DIR}/${REQUEST_ID}.json"

DECIDED_BY="${DECIDED_BY}" \
REQUEST_ID="${REQUEST_ID}" \
ACTION="${ACTION}" \
REASON="${REASON}" \
python3 - "${REQUEST_FILE}" "${DECISION_FILE}" <<'PY'
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta

request_path, decision_path = sys.argv[1], sys.argv[2]
with open(request_path, encoding="utf-8") as handle:
    request = json.load(handle)

request_id = os.environ["REQUEST_ID"]
action = os.environ["ACTION"]
decided_by = os.environ.get("DECIDED_BY", "unknown")
if request.get("id") != request_id:
    raise SystemExit("请求文件中的 id 与文件名不一致")

if request_id.startswith("op-"):
    payload = {
        "capability": str(request.get("capability", "")).strip(),
        "operation": str(request.get("operation", "")).strip(),
        "target": str(request.get("target", "")).strip(),
        "parameters": request.get("parameters", {}),
    }
else:
    payload = request.get("binding") or {
        "skill": request.get("skill", ""),
        "action": request.get("action", ""),
        "detail": request.get("detail", ""),
    }

encoded = json.dumps(
    payload,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
).encode("utf-8")
fingerprint = hashlib.sha256(encoded).hexdigest()
if request.get("fingerprint") and request.get("fingerprint") != fingerprint:
    raise SystemExit("请求指纹不匹配，拒绝裁决")

try:
    required_approvers = int(request.get("required_approvers") or 1)
except (TypeError, ValueError):
    required_approvers = 1

existing = None
if os.path.exists(decision_path):
    with open(decision_path, encoding="utf-8") as handle:
        existing = json.load(handle)
if existing and existing.get("decision") in ("approve", "deny"):
    raise SystemExit(f"请求已裁决，不允许覆盖：{decision_path}")

now = datetime.now().astimezone()
reason = os.environ.get("REASON", "").strip()

if action == "deny":
    # deny 任意时刻一票即决；可终止仍在收集票数的多票请求。
    decision = {
        "schema_version": 1,
        "request_id": request_id,
        "decision": "deny",
        "fingerprint": fingerprint,
        "decided_at": now.isoformat(timespec="seconds"),
        "decided_by": decided_by,
        "expires_at": (now + timedelta(seconds=300)).isoformat(timespec="seconds"),
    }
    if reason:
        decision["reason"] = reason
    with open(decision_path, "w", encoding="utf-8") as handle:
        json.dump(decision, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.chmod(decision_path, 0o600)
elif required_approvers > 1:
    # 多票（双签）：逐票累计，达到法定人数才翻为 approved 并刷新 expires_at。
    approvals = []
    if existing:
        approvals = [
            item for item in existing.get("approvals") or [] if isinstance(item, dict)
        ]
    voters = {
        str(item.get("decided_by")) for item in approvals if item.get("decided_by")
    }
    if decided_by in voters:
        print(f"[approve] 批准人重复投票，拒绝：{decided_by}", file=sys.stderr)
        sys.exit(3)
    approvals.append(
        {"decided_by": decided_by, "decided_at": now.isoformat(timespec="seconds")}
    )
    voters.add(decided_by)
    quorum = len(voters) >= required_approvers
    try:
        decision_ttl = int(request.get("ttl_seconds") or 300)
    except (TypeError, ValueError):
        decision_ttl = 300
    decision = {
        "schema_version": 2,
        "request_id": request_id,
        "decision": "approve" if quorum else "collecting",
        "required_approvers": required_approvers,
        "approvals": approvals,
        "fingerprint": fingerprint,
        "decided_at": now.isoformat(timespec="seconds"),
        "decided_by": decided_by,
        "expires_at": (now + timedelta(seconds=max(1, decision_ttl))).isoformat(
            timespec="seconds"
        ),
    }
    if request.get("second_confirmation_required"):
        decision["second_confirmation_required"] = True
    if reason:
        decision["reason"] = reason
    with open(decision_path, "w", encoding="utf-8") as handle:
        json.dump(decision, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.chmod(decision_path, 0o600)
else:
    # 单签：与历史版本完全一致，独占创建、禁止覆盖。
    decision = {
        "schema_version": 1,
        "request_id": request_id,
        "decision": "approve",
        "fingerprint": fingerprint,
        "decided_at": now.isoformat(timespec="seconds"),
        "decided_by": decided_by,
        "expires_at": (now + timedelta(seconds=300)).isoformat(timespec="seconds"),
    }
    if reason:
        decision["reason"] = reason
    with open(decision_path, "x", encoding="utf-8") as handle:
        json.dump(decision, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.chmod(decision_path, 0o600)

output = {
    "id": request_id,
    "decision": decision["decision"],
    "fingerprint": fingerprint,
    "summary": request.get("summary") or request.get("detail"),
    "target": request.get("target"),
    "operation": request.get("operation") or request.get("action"),
}
if required_approvers > 1 and action == "approve":
    output["approvals"] = len(decision.get("approvals") or [])
    output["required_approvers"] = required_approvers
print(json.dumps(output, ensure_ascii=False, indent=2))
PY

printf '[%s] [%s] request=%s by=%s reason=%s\n' \
  "$(date '+%F %T')" "${ACTION}" "${REQUEST_ID}" "${DECIDED_BY}" "${REASON}" \
  | tee -a "${LOG_FILE}"
