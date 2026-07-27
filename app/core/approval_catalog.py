"""Unified pending approval catalogue for operation and authorization requests."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import operations, owner_approval


def _expired(value: object) -> bool:
    try:
        timestamp = datetime.fromisoformat(str(value))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) > timestamp.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return True


def _pending_authorizations(root: str | Path | None = None) -> list[dict[str, Any]]:
    paths = owner_approval.approval_paths(root)
    directory = paths["auth_requests"]
    rows: list[dict[str, Any]] = []
    if not directory.is_dir():
        return rows
    consumed = owner_approval.runtime_root(root) / "data" / "auth-consumed"
    for path in directory.glob("auth-*.json"):
        request = owner_approval.read_json(path)
        if not request:
            continue
        request_id = str(request.get("id", ""))
        if (consumed / f"{request_id}.json").is_file():
            continue
        decision = owner_approval.read_json(paths["decisions"] / f"{request_id}.json")
        if decision and decision.get("decision") in {"approve", "deny"}:
            continue
        if _expired(request.get("expires_at")):
            continue
        try:
            fingerprint = owner_approval.request_fingerprint(request, request_id)
        except owner_approval.ApprovalError:
            continue
        binding = request.get("binding", {}) if isinstance(request.get("binding"), dict) else {}
        rows.append(
            {
                "id": request_id,
                "kind": "authorization",
                "fingerprint": fingerprint,
                "created_at": request.get("created_at", ""),
                "summary": request.get("detail") or request.get("reason") or "",
                "target": binding.get("session_id") or binding.get("goal_sha256") or "Agenelf",
                "operation": request.get("action") or binding.get("kind") or "authorize",
                "risk": request.get("risk") or request.get("approval_mode") or "owner_exact",
                "mtime": path.stat().st_mtime,
            }
        )
    return rows


def _pending_operations(root: str | Path | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in owner_approval.list_pending_operations(root):
        request_id = str(item.get("id", ""))
        try:
            request = owner_approval.load_request(request_id, root)
        except owner_approval.ApprovalError:
            continue
        if operations.request_expired(request, fail_closed=True):
            continue
        rows.append(dict(item, kind="operation", mtime=0.0))
    return rows


def list_pending_requests(root: str | Path | None = None) -> list[dict[str, Any]]:
    rows = _pending_operations(root)
    rows.extend(_pending_authorizations(root))
    rows.sort(
        key=lambda item: (str(item.get("created_at", "")), float(item.get("mtime", 0))),
        reverse=True,
    )
    for row in rows:
        row.pop("mtime", None)
    return rows


def _explicit_request_is_live(
    request_id: str,
    request: dict[str, Any],
) -> None:
    if request_id.startswith("op-"):
        if operations.request_expired(request, fail_closed=True):
            raise owner_approval.ApprovalError(
                f"运维请求 {request_id} 已过期，不能再批准；请重新提交当前操作"
            )
        return
    if _expired(request.get("expires_at")):
        raise owner_approval.ApprovalError(
            f"授权请求 {request_id} 已过期，不能再批准；请重新发起授权流程"
        )


def resolve_pending_request(
    request_id: str | None = None,
    root: str | Path | None = None,
) -> tuple[dict[str, Any], list[str]]:
    normalized = str(request_id or "").strip().lower()
    pending = list_pending_requests(root)
    if normalized in {"", "latest", "newest", "最新", "刚才", "该请求", "这个请求"}:
        if not pending:
            raise owner_approval.ApprovalError("当前没有等待主人审批的请求")
        if len(pending) == 1:
            return pending[0], []
        fingerprints = {str(item.get("fingerprint", "")) for item in pending}
        if len(fingerprints) == 1:
            return pending[0], [str(item["id"]) for item in pending[1:]]
        raise owner_approval.AmbiguousApprovalError(
            "存在多个不同载荷的待审批请求，请明确输入请求 ID",
            pending=pending[:20],
        )

    rid = owner_approval.validate_request_id(normalized)
    request = owner_approval.load_request(rid, root)
    _explicit_request_is_live(rid, request)
    selected = next((item for item in pending if str(item.get("id", "")) == rid), None)
    if selected is None:
        if rid.startswith("op-"):
            state = operations.get_operation(rid, root=Path(root).resolve() if root else None)
            raise owner_approval.ApprovalError(
                f"请求 {rid} 当前不在等待审批状态：{state.get('status', 'unknown')}"
            )
        raise owner_approval.ApprovalError(f"请求 {rid} 当前不在等待审批状态")
    duplicates = [
        str(item["id"])
        for item in pending
        if item.get("id") != rid and item.get("fingerprint") == selected.get("fingerprint")
    ]
    return selected, duplicates
