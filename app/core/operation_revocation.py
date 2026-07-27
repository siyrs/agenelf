"""Owner-only revocation for operation requests that have not started.

The Agent and model never receive a revocation tool.  A host-side owner command uses
this module to race on the same per-request lock as ``ops-runner``.  If revocation wins,
it writes an immutable trusted ``cancelled`` result before releasing the lock; the runner
then observes the result and skips the request.  If the runner already owns the lock,
revocation fails closed and reports that execution may already have started.
"""
from __future__ import annotations

import hmac
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import operations
from core.storage import atomic_write_json as _atomic_json
from core.privacy import redact_sensitive_text

_ID_RE = re.compile(r"op-[0-9a-f]{16}")
_ACTOR_RE = re.compile(r"[A-Za-z0-9._@:-]{1,96}")
_MAX_REASON = 1000


class OperationRevocationError(RuntimeError):
    """Raised when an operation cannot be safely revoked."""


def _now(at: datetime | None = None) -> datetime:
    value = at or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _root(root: str | Path | None = None) -> Path:
    if root is not None:
        return Path(root).resolve()
    return operations.runtime_root()


def _validate_id(value: object) -> str:
    operation_id = str(value or "").strip().lower()
    if not _ID_RE.fullmatch(operation_id):
        raise OperationRevocationError(f"非法操作 ID：{value!r}")
    return operation_id


def _sanitize_reason(value: object) -> str:
    text = redact_sensitive_text(value)
    return " ".join(str(text).strip().split())[:_MAX_REASON]


def _validate_actor(value: object) -> str:
    actor = str(value or "owner-host").strip()
    if not _ACTOR_RE.fullmatch(actor):
        raise OperationRevocationError(f"非法主人标识：{value!r}")
    return actor


def _load_valid_request(operation_id: str, root: Path) -> dict[str, Any]:
    paths = operations.queue_paths(root)
    request = operations.read_json(paths["requests"] / f"{operation_id}.json")
    if request is None:
        raise OperationRevocationError(f"请求不存在或不是有效 JSON：{operation_id}")
    if str(request.get("id", "")) != operation_id:
        raise OperationRevocationError("请求文件中的 id 与文件名不一致")
    parameters = request.get("parameters", {})
    if not isinstance(parameters, dict):
        raise OperationRevocationError("request.parameters 必须是对象")
    payload = operations.canonical_payload(
        str(request.get("capability", "")),
        str(request.get("operation", "")),
        str(request.get("target", "")),
        parameters,
    )
    expected = operations.payload_fingerprint(payload)
    if not hmac.compare_digest(str(request.get("fingerprint", "")), expected):
        raise OperationRevocationError("请求指纹不匹配，拒绝撤销")
    return request


def _existing_cancellation(
    result: dict[str, Any] | None,
    operation_id: str,
) -> dict[str, Any] | None:
    if not result or str(result.get("status", "")) != "cancelled":
        return None
    cancellation = result.get("cancellation", {})
    return {
        "id": operation_id,
        "status": "cancelled",
        "idempotent": True,
        "cancelled_at": cancellation.get("cancelled_at") or result.get("finished_at"),
        "cancelled_by": cancellation.get("cancelled_by"),
        "reason": cancellation.get("reason", ""),
        "started": False,
    }


def operation_control_status(
    operation_id: str,
    *,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Return a secret-free status view suitable for the Agent and CLI output."""

    rid = _validate_id(operation_id)
    base = _root(root)
    paths = operations.queue_paths(base)
    request = operations.read_json(paths["requests"] / f"{rid}.json")
    if request is None:
        return {"id": rid, "status": "not_found", "revocable": False}
    result = operations.read_json(paths["results"] / f"{rid}.json")
    decision = operations.read_json(paths["decisions"] / f"{rid}.json")
    expired = operations.request_expired(request, fail_closed=True)
    executing = (paths["locks"] / f"{rid}.lock").exists() and result is None
    decision_value = str((decision or {}).get("decision", ""))
    revocable = bool(
        result is None
        and not expired
        and not executing
        and decision_value != "deny"
    )
    value: dict[str, Any] = {
        "id": rid,
        "status": str((result or {}).get("status") or operations.get_operation(rid, root=base).get("status")),
        "capability": str(request.get("capability", "")),
        "operation": str(request.get("operation", "")),
        "target": str(request.get("target", "")),
        "risk": str(request.get("risk", "")),
        "summary": _sanitize_reason(request.get("summary", "")),
        "created_at": request.get("created_at"),
        "expires_at": request.get("expires_at"),
        "decision": decision_value or None,
        "executing": executing,
        "revocable": revocable,
    }
    if result and str(result.get("status", "")) == "cancelled":
        cancellation = result.get("cancellation", {})
        value["cancellation"] = {
            "cancelled_at": cancellation.get("cancelled_at"),
            "cancelled_by": cancellation.get("cancelled_by"),
            "reason": _sanitize_reason(cancellation.get("reason", "")),
            "started": False,
        }
    return value


def list_revocable_operations(
    *,
    root: str | Path | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List live requests that can still be atomically cancelled before execution."""

    base = _root(root)
    paths = operations.queue_paths(base)
    directory = paths["requests"]
    if not directory.is_dir():
        return []
    rows: list[tuple[float, dict[str, Any]]] = []
    bounded = max(1, min(int(limit), 200))
    for path in directory.glob("op-*.json"):
        try:
            status = operation_control_status(path.stem, root=base)
        except OperationRevocationError:
            continue
        if not status.get("revocable"):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        rows.append((mtime, status))
    rows.sort(key=lambda item: (str(item[1].get("created_at", "")), item[0]), reverse=True)
    return [item[1] for item in rows[:bounded]]


def revocation_instructions(operation_id: str) -> str:
    rid = _validate_id(operation_id)
    return (
        f"Windows PowerShell：.\\scripts\\revoke.ps1 {rid}\n"
        f"跨平台 Python：python scripts/revoke.py {rid}\n"
        f"Linux/macOS：bash scripts/revoke.sh {rid}\n"
        "撤销仅对尚未开始的请求生效；若 Runner 已取得执行锁，会失败关闭并提示已开始。"
    )


def revoke_operation(
    operation_id: str,
    reason: str = "",
    cancelled_by: str = "owner-host",
    *,
    root: str | Path | None = None,
    at: datetime | None = None,
) -> dict[str, Any]:
    """Atomically cancel one operation before the deterministic runner starts it."""

    rid = _validate_id(operation_id)
    actor = _validate_actor(cancelled_by)
    clean_reason = _sanitize_reason(reason)
    base = _root(root)
    paths = operations.queue_paths(base)
    request = _load_valid_request(rid, base)
    result_path = paths["results"] / f"{rid}.json"
    existing = operations.read_json(result_path)
    idempotent = _existing_cancellation(existing, rid)
    if idempotent is not None:
        return idempotent
    if existing is not None:
        raise OperationRevocationError(
            f"请求 {rid} 已有可信终态 {existing.get('status', 'finished')}，不能撤销"
        )
    if operations.request_expired(request, at=at, fail_closed=True):
        raise OperationRevocationError(f"请求 {rid} 已过期，无需撤销；Runner 会写入 expired 终态")
    decision = operations.read_json(paths["decisions"] / f"{rid}.json")
    if decision and decision.get("decision") == "deny":
        raise OperationRevocationError(f"请求 {rid} 已被拒绝，不会执行")

    lock_path = paths["locks"] / f"{rid}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    except FileExistsError as exc:
        raise OperationRevocationError(
            f"请求 {rid} 已被 Runner 取得执行锁，可能已经开始；不能宣称撤销成功"
        ) from exc

    try:
        request = _load_valid_request(rid, base)
        existing = operations.read_json(result_path)
        idempotent = _existing_cancellation(existing, rid)
        if idempotent is not None:
            return idempotent
        if existing is not None:
            raise OperationRevocationError(
                f"请求 {rid} 已在并发处理中完成为 {existing.get('status', 'finished')}"
            )
        if operations.request_expired(request, at=at, fail_closed=True):
            raise OperationRevocationError(f"请求 {rid} 已在撤销竞争期间过期")
        timestamp = _now(at).isoformat(timespec="seconds")
        cancellation = {
            "request_id": rid,
            "request_fingerprint": str(request.get("fingerprint", "")),
            "cancelled_at": timestamp,
            "cancelled_by": actor,
            "reason": clean_reason,
            "started": False,
        }
        result = {
            "schema_version": 2,
            "id": rid,
            "status": "cancelled",
            "capability": str(request.get("capability", "")),
            "operation": str(request.get("operation", "")),
            "target": str(request.get("target", "")),
            "finished_at": timestamp,
            "commands": [],
            "cancellation": cancellation,
        }
        _atomic_json(result_path, result, exclusive=True)
        operations.audit(
            "operation_cancelled",
            f"{rid} by={actor} started=false reason={clean_reason}",
            base,
        )
        return {
            "id": rid,
            "status": "cancelled",
            "idempotent": False,
            "cancelled_at": timestamp,
            "cancelled_by": actor,
            "reason": clean_reason,
            "started": False,
        }
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass
