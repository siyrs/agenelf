"""File-backed operation queue shared by the Agent and the privileged runner.

The LLM-facing Agent can only *propose* operations by writing immutable request
files.  A separate deterministic runner owns SSH credentials and writes result
files.  Human approval decisions are stored in another directory mounted
read-only into the Agent container.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

RISK_READ = "read"
RISK_CHANGE = "change"
RISK_PRIVILEGED = "privileged"
RISK_FORBIDDEN = "forbidden"
VALID_RISKS = {RISK_READ, RISK_CHANGE, RISK_PRIVILEGED, RISK_FORBIDDEN}

_ID_RE = re.compile(r"op-[0-9a-f]{16}")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def runtime_root() -> Path:
    configured = os.environ.get("AGENELF_ROOT", "").strip()
    if configured:
        return Path(configured).resolve()
    return Path(__file__).resolve().parents[2]


def queue_paths(root: Path | None = None) -> dict[str, Path]:
    base = root or runtime_root()
    data = base / "data"
    return {
        "requests": data / "ops-requests",
        "results": data / "ops-results",
        "decisions": data / "auth-decisions",
        "locks": data / "ops-locks",
        "audit": base / "logs" / "operations.log",
    }


def canonical_payload(
    capability: str,
    operation: str,
    target: str,
    parameters: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return the exact payload covered by an approval decision."""

    return {
        "capability": str(capability).strip(),
        "operation": str(operation).strip(),
        "target": str(target).strip(),
        "parameters": parameters or {},
    }


def payload_fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_operation_id(operation_id: str) -> str:
    value = str(operation_id or "").strip()
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"非法操作 ID：{operation_id!r}")
    return value


def _atomic_write_json(path: Path, data: dict[str, Any], exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if exclusive:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        return
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def audit(event: str, detail: str, root: Path | None = None) -> None:
    path = queue_paths(root)["audit"]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{now_iso()}] [{event}] {detail}\n")
    except OSError:
        # Auditing must never crash the chat path.  The runner has its own audit.
        pass


def submit_operation(
    capability: str,
    operation: str,
    target: str,
    parameters: dict[str, Any] | None,
    risk: str,
    summary: str,
    root: Path | None = None,
) -> dict[str, Any]:
    """Create an operation request with an approval-bound fingerprint."""

    risk = str(risk).lower().strip()
    if risk not in VALID_RISKS:
        raise ValueError(f"未知风险级别：{risk}")
    if risk == RISK_FORBIDDEN:
        raise PermissionError("安全红线禁止提交该操作")
    if not str(operation).strip() or not str(target).strip():
        raise ValueError("operation 与 target 不能为空")
    if parameters is not None and not isinstance(parameters, dict):
        raise TypeError("parameters 必须是对象")

    payload = canonical_payload(capability, operation, target, parameters)
    json.dumps(payload, ensure_ascii=False, sort_keys=True)
    operation_id = f"op-{uuid.uuid4().hex[:16]}"
    request = {
        "schema_version": 1,
        "id": operation_id,
        **payload,
        "risk": risk,
        "summary": str(summary).strip(),
        "fingerprint": payload_fingerprint(payload),
        "created_at": now_iso(),
        "created_by": "agenelf-agent",
    }
    path = queue_paths(root)["requests"] / f"{operation_id}.json"
    _atomic_write_json(path, request, exclusive=True)
    audit(
        "operation_submitted",
        f"{operation_id} {capability}.{operation} target={target} risk={risk}",
        root,
    )
    return request


def get_operation(operation_id: str, root: Path | None = None) -> dict[str, Any]:
    """Return a safe combined view of request, approval, and trusted result."""

    operation_id = _validate_operation_id(operation_id)
    paths = queue_paths(root)
    request = read_json(paths["requests"] / f"{operation_id}.json")
    if request is None:
        return {"id": operation_id, "status": "not_found"}

    result = read_json(paths["results"] / f"{operation_id}.json")
    if result is not None:
        return {
            "id": operation_id,
            "status": str(result.get("status", "finished")),
            "request": request,
            "result": result,
        }

    decision = read_json(paths["decisions"] / f"{operation_id}.json")
    risk = request.get("risk")
    if decision:
        decision_value = decision.get("decision")
        status = "approved" if decision_value == "approve" else "denied"
    elif risk == RISK_READ:
        status = "queued"
    else:
        status = "awaiting_approval"
    return {
        "id": operation_id,
        "status": status,
        "request": request,
        "decision": decision,
    }


def wait_for_result(
    operation_id: str,
    timeout_seconds: float = 0,
    poll_interval: float = 0.2,
    root: Path | None = None,
) -> dict[str, Any]:
    """Wait briefly for the runner, mainly for read-only chat operations."""

    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while True:
        current = get_operation(operation_id, root=root)
        if current.get("result") is not None or current.get("status") in {
            "denied",
            "failed",
            "blocked",
            "not_found",
        }:
            return current
        if time.monotonic() >= deadline:
            return current
        time.sleep(max(0.05, float(poll_interval)))
