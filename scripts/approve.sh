#!/usr/bin/env bash
# approve.sh — host-only decision gate for exact Agenelf operation requests.
#
# Usage:
#   bash scripts/approve.sh <op-or-auth-id> [approve|deny] [reason]
#
# The script never edits the Agent-writable request.  It creates a separate
# decision document in data/auth-decisions/.  That directory must be mounted
# read-only into the Agent container and read-only into the ops runner.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REQUEST_ID="${1:-}"
ACTION="${2:-approve}"
REASON="${3:-}"
DECISIONS_DIR="${ROOT_DIR}/data/auth-decisions"
LOG_FILE="${ROOT_DIR}/logs/audit.log"

usage() {
  echo "用法：bash scripts/approve.sh <op-or-auth-id> [approve|deny] [reason]" >&2
  exit 2
}

[[ -n "${REQUEST_ID}" ]] || usage
[[ "${REQUEST_ID}" =~ ^(op-[0-9a-f]{16}|auth-[0-9a-f]{12})$ ]] || {
  echo "[approve] 非法请求 ID：${REQUEST_ID}" >&2
  exit 1
}
[[ "${ACTION}" == "approve" || "${ACTION}" == "deny" ]] || usage

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
[[ ! -e "${DECISION_FILE}" ]] || {
  echo "[approve] 请求已裁决，不允许覆盖：${DECISION_FILE}" >&2
  exit 1
}

DECIDED_BY="${USER:-unknown}" \
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

now = datetime.now().astimezone()
decision = {
    "schema_version": 1,
    "request_id": request_id,
    "decision": action,
    "fingerprint": fingerprint,
    "decided_at": now.isoformat(timespec="seconds"),
    "decided_by": os.environ.get("DECIDED_BY", "unknown"),
    "expires_at": (now + timedelta(seconds=300)).isoformat(timespec="seconds"),
}
reason = os.environ.get("REASON", "").strip()
if reason:
    decision["reason"] = reason

with open(decision_path, "x", encoding="utf-8") as handle:
    json.dump(decision, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
os.chmod(decision_path, 0o600)

print(json.dumps({
    "id": request_id,
    "decision": action,
    "fingerprint": fingerprint,
    "summary": request.get("summary") or request.get("detail"),
    "target": request.get("target"),
    "operation": request.get("operation") or request.get("action"),
}, ensure_ascii=False, indent=2))
PY

printf '[%s] [%s] request=%s by=%s reason=%s\n' \
  "$(date '+%F %T')" "${ACTION}" "${REQUEST_ID}" "${USER:-unknown}" "${REASON}" \
  | tee -a "${LOG_FILE}"
