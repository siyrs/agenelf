"""Unified pending approval catalogue for operation and authorization requests."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import owner_approval


def _expired(value: object) -> bool:
    try:
        timestamp = datetime.fromisoformat(str(value))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) > timestamp.astimezone(timezone.utc)
    except ValueError:
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


def list_pending_requests(root: str | Path | None = None) -> list[dict[str, Any]]:
    rows = [dict(item, kind="operation") for item in owner_approval.list_pending_operations(root)]
    rows.extend(_pending_authorizations(root))
    rows.sort(
        key=lambda item: (str(item.get("created_at", "")), float(item.get("mtime", 0))),
        reverse=True,
    )
    for row in rows:
        row.pop("mtime", None)
    return rows


def resolve_pending_request(
    request_id: str | None = None,
    root: str | Path | None = None,
) -> tuple[dict[str, Any], list[str]]:
    normalized = str(request_id or "").strip().lower()
    if normalized not in {"", "latest", "newest", "最新", "刚才", "该请求", "这个请求"}:
        selected, duplicates = owner_approval.resolve_pending_operation(normalized, root)
        selected = dict(selected)
        selected["kind"] = "operation" if str(selected.get("id", "")).startswith("op-") else "authorization"
        return selected, duplicates

    pending = list_pending_requests(root)
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
