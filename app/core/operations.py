"""File-backed operation queue shared by the Agent and the privileged runner.

The LLM-facing Agent can only *propose* operations by writing immutable request
files. A separate deterministic runner owns SSH credentials and writes result
files. Human approval decisions are stored in another directory mounted
read-only into the Agent container.

Operation requests are time-bounded and identical unfinished requests are
reused. This prevents an old approval from authorizing a stale change and keeps
repeated model/tool attempts from creating an unbounded pile of duplicate
requests.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

RISK_READ = "read"
RISK_CHANGE = "change"
RISK_PRIVILEGED = "privileged"
RISK_FORBIDDEN = "forbidden"
VALID_RISKS = {RISK_READ, RISK_CHANGE, RISK_PRIVILEGED, RISK_FORBIDDEN}

_ID_RE = re.compile(r"op-[0-9a-f]{16}")
_DEFAULT_TTL_SECONDS = {
    RISK_READ: 120,
    RISK_CHANGE: 1800,
    RISK_PRIVILEGED: 900,
}
_TTL_ENV = {
    RISK_READ: "AGENELF_OPERATION_READ_TTL_SECONDS",
    RISK_CHANGE: "AGENELF_OPERATION_CHANGE_TTL_SECONDS",
    RISK_PRIVILEGED: "AGENELF_OPERATION_PRIVILEGED_TTL_SECONDS",
}
_MAX_TTL_SECONDS = 86_400


def _now(at: datetime | None = None) -> datetime:
    value = at or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def now_iso(at: datetime | None = None) -> str:
    return _now(at).isoformat(timespec="seconds")


def _parse_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def request_ttl_seconds(risk: str, explicit: object | None = None) -> int:
    """Resolve a bounded request lifetime for one effective risk level."""

    normalized = str(risk or "").strip().lower()
    default = _DEFAULT_TTL_SECONDS.get(normalized, _DEFAULT_TTL_SECONDS[RISK_CHANGE])
    if explicit is None:
        explicit = os.environ.get(_TTL_ENV.get(normalized, ""), default)
    return _bounded_int(explicit, default, 15, _MAX_TTL_SECONDS)


def request_expiry(request: dict[str, Any]) -> datetime | None:
    """Return the explicit or legacy-derived request expiry timestamp."""

    explicit = _parse_time(request.get("expires_at"))
    if explicit is not None:
        return explicit
    created = _parse_time(request.get("created_at"))
    if created is None:
        return None
    risk = str(request.get("risk", RISK_CHANGE)).strip().lower()
    ttl = request_ttl_seconds(risk, request.get("ttl_seconds"))
    return created + timedelta(seconds=ttl)


def request_expired(
    request: dict[str, Any],
    *,
    at: datetime | None = None,
    fail_closed: bool = False,
) -> bool:
    expiry = request_expiry(request)
    if expiry is None:
        return bool(fail_closed)
    return _now(at) > expiry


def decision_expired(decision: dict[str, Any] | None, *, at: datetime | None = None) -> bool:
    if not decision:
        return False
    expiry = _parse_time(decision.get("expires_at"))
    return expiry is None or _now(at) > expiry


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


_POLICY_RISK_ORDER = {
    RISK_READ: 0,
    RISK_CHANGE: 1,
    RISK_PRIVILEGED: 2,
    "irreversible": 3,
    RISK_FORBIDDEN: 4,
}


def _policy_evaluation(capability: str, operation: str) -> dict[str, Any] | None:
    """Consult the policy engine; return None when it is unavailable."""

    try:
        from core.policy import PolicyEngine  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        engine = PolicyEngine()
        if getattr(engine, "degraded", False):
            return None
        result = engine.evaluate(capability, operation, subject="agent")
    except Exception:
        return None
    return result if isinstance(result, dict) else None


def _strictest_risk(declared: str, evaluation: dict[str, Any]) -> str:
    """Return the stricter of the declared and policy-evaluated risks."""

    policy_risk = str(evaluation.get("risk") or "").lower().strip()
    if policy_risk in ("irreversible", RISK_FORBIDDEN):
        raise PermissionError(f"策略引擎判定风险 {policy_risk} 超出可提交范围，拒绝提交")
    effective = declared
    if (
        policy_risk in _POLICY_RISK_ORDER
        and _POLICY_RISK_ORDER[policy_risk] > _POLICY_RISK_ORDER[declared]
    ):
        effective = policy_risk
    if effective == RISK_READ and evaluation.get("auto_execute") is False:
        effective = RISK_CHANGE
    return effective


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
        # Auditing must never crash the chat path. The runner has its own audit.
        pass


def _request_payload_matches(request: dict[str, Any], fingerprint: str) -> bool:
    parameters = request.get("parameters", {})
    if not isinstance(parameters, dict):
        return False
    payload = canonical_payload(
        str(request.get("capability", "")),
        str(request.get("operation", "")),
        str(request.get("target", "")),
        parameters,
    )
    return payload_fingerprint(payload) == fingerprint == str(request.get("fingerprint", ""))


def find_reusable_operation(
    payload: dict[str, Any],
    risk: str,
    *,
    root: Path | None = None,
    at: datetime | None = None,
) -> dict[str, Any] | None:
    """Find an identical non-terminal request that is still safe to reuse."""

    paths = queue_paths(root)
    directory = paths["requests"]
    if not directory.is_dir():
        return None
    fingerprint = payload_fingerprint(payload)
    candidates = sorted(
        directory.glob("op-*.json"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        request = read_json(path)
        if not request or request.get("id") != path.stem:
            continue
        if str(request.get("risk", "")) != risk:
            continue
        if not _request_payload_matches(request, fingerprint):
            continue
        request_id = str(request["id"])
        if (paths["results"] / f"{request_id}.json").is_file():
            continue
        if request_expired(request, at=at, fail_closed=True):
            continue
        decision = read_json(paths["decisions"] / f"{request_id}.json")
        if decision:
            state = str(decision.get("decision", ""))
            if state == "deny":
                continue
            if state in {"approve", "collecting"} and decision_expired(decision, at=at):
                continue
        reused = dict(request)
        reused["reused_existing"] = True
        reused["reuse_reason"] = "identical_unfinished_request"
        return reused
    return None


def submit_operation(
    capability: str,
    operation: str,
    target: str,
    parameters: dict[str, Any] | None,
    risk: str,
    summary: str,
    root: Path | None = None,
    *,
    ttl_seconds: int | None = None,
    deduplicate: bool = True,
) -> dict[str, Any]:
    """Create or reuse an approval-bound, time-limited operation request."""

    risk = str(risk).lower().strip()
    if risk not in VALID_RISKS:
        raise ValueError(f"未知风险级别：{risk}")
    if risk == RISK_FORBIDDEN:
        raise PermissionError("安全红线禁止提交该操作")
    if not str(operation).strip() or not str(target).strip():
        raise ValueError("operation 与 target 不能为空")
    if parameters is not None and not isinstance(parameters, dict):
        raise TypeError("parameters 必须是对象")

    declared_risk = risk
    evaluation = _policy_evaluation(capability, operation)
    policy_version = ""
    approval_mode = ""
    if evaluation:
        policy_version = str(evaluation.get("policy_version") or "")
        approval_mode = str(evaluation.get("approval") or "")
        if evaluation.get("allowed") is False or approval_mode == "impossible":
            reason = str(evaluation.get("reason") or "策略禁止")
            raise PermissionError(f"策略引擎拒绝提交该操作：{reason}")
        risk = _strictest_risk(declared_risk, evaluation)

    payload = canonical_payload(capability, operation, target, parameters)
    json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if deduplicate:
        reusable = find_reusable_operation(payload, risk, root=root)
        if reusable is not None:
            audit(
                "operation_reused",
                f"{reusable['id']} {capability}.{operation} target={target} risk={risk}",
                root,
            )
            return reusable

    created = _now()
    ttl = request_ttl_seconds(risk, ttl_seconds)
    operation_id = f"op-{uuid.uuid4().hex[:16]}"
    request = {
        "schema_version": 1,
        "id": operation_id,
        **payload,
        "risk": risk,
        "summary": str(summary).strip(),
        "fingerprint": payload_fingerprint(payload),
        "created_at": now_iso(created),
        "expires_at": now_iso(created + timedelta(seconds=ttl)),
        "ttl_seconds": ttl,
        "created_by": "agenelf-agent",
    }
    if evaluation:
        request["policy_version"] = policy_version
        request["approval_mode"] = approval_mode
        if risk != declared_risk:
            request["declared_risk"] = declared_risk
    path = queue_paths(root)["requests"] / f"{operation_id}.json"
    _atomic_write_json(path, request, exclusive=True)
    audit(
        "operation_submitted",
        f"{operation_id} {capability}.{operation} target={target} risk={risk} ttl={ttl}",
        root,
    )
    return request


def approval_instructions(operation_id: str) -> str:
    """Return platform-neutral, owner-only approval guidance."""

    operation_id = _validate_operation_id(operation_id)
    return (
        f"当前 Agenelf CLI：/approve {operation_id}\n"
        f"中文输入：审批通过 {operation_id}\n"
        f"Windows PowerShell 备用：.\\scripts\\approve.ps1 {operation_id} approve\n"
        f"跨平台 Python 备用：python scripts/approve.py {operation_id} approve"
    )


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

    expiry = request_expiry(request)
    if request_expired(request, fail_closed=True):
        return {
            "id": operation_id,
            "status": "expired",
            "request": request,
            "expired_at": expiry.isoformat(timespec="seconds") if expiry else None,
            "next_action": "重新提交当前操作以生成新的精确请求和审批窗口",
        }

    decision = read_json(paths["decisions"] / f"{operation_id}.json")
    risk = request.get("risk")
    if decision:
        decision_value = str(decision.get("decision", ""))
        if decision_value == "deny":
            status = "denied"
        elif decision_value in {"approve", "collecting"} and decision_expired(decision):
            status = "approval_expired"
        elif decision_value == "approve":
            status = "approved"
        elif decision_value == "collecting":
            status = "collecting_approval"
        else:
            status = "awaiting_approval"
    elif risk == RISK_READ:
        status = "queued"
    else:
        status = "awaiting_approval"
    value = {
        "id": operation_id,
        "status": status,
        "request": request,
        "decision": decision,
    }
    if status == "approval_expired":
        value["next_action"] = "原审批窗口已过期；重新提交操作，不要复用旧请求"
    return value


def wait_for_result(
    operation_id: str,
    timeout_seconds: float = 0,
    poll_interval: float = 0.2,
    root: Path | None = None,
) -> dict[str, Any]:
    """Wait briefly for the runner, mainly for read-only chat operations."""

    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    terminal = {
        "denied",
        "failed",
        "blocked",
        "expired",
        "approval_expired",
        "not_found",
    }
    while True:
        current = get_operation(operation_id, root=root)
        if current.get("result") is not None or current.get("status") in terminal:
            return current
        if time.monotonic() >= deadline:
            return current
        time.sleep(max(0.05, float(poll_interval)))
