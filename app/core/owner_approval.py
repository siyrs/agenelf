"""Cross-platform owner approval control plane.

The model-facing Agent is never allowed to write final authorization decisions. A
raw interactive CLI command creates a short-lived, HMAC-signed owner command in a
separate queue. A deterministic approval runner validates the signature and the
current request fingerprint before writing ``data/auth-decisions/<request-id>.json``.

Host operators may call :func:`apply_owner_decision` directly through
``scripts/approve.py``; this keeps Windows, macOS and Linux behavior identical.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.storage import atomic_write_json as _atomic_json
from core.storage import read_json as _read_storage_json

_REQUEST_ID_RE = re.compile(r"(?:op-[0-9a-f]{16}|auth-[0-9a-f]{12})")
_COMMAND_ID_RE = re.compile(r"owner-decision-[0-9a-f]{16}")
_ACTOR_RE = re.compile(r"[A-Za-z0-9._@:-]{1,96}")
_FINAL_DECISIONS = {"approve", "deny"}
_MAX_REASON = 1000
_DEFAULT_DECISION_TTL = 300
_DEFAULT_COMMAND_TTL = 120


class ApprovalError(RuntimeError):
    """Raised when an approval request cannot be safely accepted."""


class AmbiguousApprovalError(ApprovalError):
    """Raised when text approval does not identify one exact pending request."""

    def __init__(self, message: str, pending: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.pending = list(pending or [])


def now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def runtime_root(root: str | Path | None = None) -> Path:
    if root is not None:
        return Path(root).resolve()
    configured = os.environ.get("AGENELF_ROOT", "").strip()
    return Path(configured).resolve() if configured else Path(__file__).resolve().parents[2]


def approval_paths(root: str | Path | None = None) -> dict[str, Path]:
    base = runtime_root(root)
    data = base / "data"
    return {
        "ops_requests": data / "ops-requests",
        "auth_requests": data / "auth-requests",
        "decisions": data / "auth-decisions",
        "operation_results": data / "ops-results",
        "commands": data / "approval-commands",
        "command_results": data / "approval-results",
        "command_locks": data / "approval-locks",
        "audit": base / "logs" / "audit.log",
    }


def _sanitize_text(value: object, limit: int = _MAX_REASON) -> str:
    text = " ".join(str(value or "").strip().split())
    return text[: max(0, int(limit))]


def read_json(path: Path) -> dict[str, Any] | None:
    value = _read_storage_json(path)
    return value if isinstance(value, dict) else None


def _audit(root: str | Path | None, event: str, detail: str) -> None:
    path = approval_paths(root)["audit"]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{now_iso()}] [{event}] {_sanitize_text(detail, 2000)}\n")
    except OSError:
        pass


def validate_request_id(value: object) -> str:
    request_id = str(value or "").strip().lower()
    if not _REQUEST_ID_RE.fullmatch(request_id):
        raise ApprovalError(f"非法请求 ID：{value!r}")
    return request_id


def _request_path(request_id: str, root: str | Path | None = None) -> Path:
    paths = approval_paths(root)
    directory = paths["ops_requests"] if request_id.startswith("op-") else paths["auth_requests"]
    return directory / f"{request_id}.json"


def load_request(request_id: object, root: str | Path | None = None) -> dict[str, Any]:
    normalized = validate_request_id(request_id)
    path = _request_path(normalized, root)
    request = read_json(path)
    if request is None:
        raise ApprovalError(f"请求不存在或不是有效 JSON：{path}")
    if str(request.get("id", "")).strip() != normalized:
        raise ApprovalError("请求文件中的 id 与文件名不一致")
    return request


def canonical_binding(request: dict[str, Any], request_id: str | None = None) -> dict[str, Any]:
    rid = request_id or str(request.get("id", ""))
    if rid.startswith("op-"):
        parameters = request.get("parameters", {})
        if not isinstance(parameters, dict):
            raise ApprovalError("operation request.parameters 必须是对象")
        return {
            "capability": str(request.get("capability", "")).strip(),
            "operation": str(request.get("operation", "")).strip(),
            "target": str(request.get("target", "")).strip(),
            "parameters": parameters,
        }
    binding = request.get("binding")
    if isinstance(binding, dict):
        return binding
    return {
        "skill": str(request.get("skill", "")),
        "action": str(request.get("action", "")),
        "detail": str(request.get("detail", "")),
    }


def binding_fingerprint(binding: dict[str, Any]) -> str:
    encoded = json.dumps(
        binding,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def request_fingerprint(request: dict[str, Any], request_id: str | None = None) -> str:
    fingerprint = binding_fingerprint(canonical_binding(request, request_id))
    declared = str(request.get("fingerprint", "")).strip()
    if declared and not hmac.compare_digest(declared, fingerprint):
        raise ApprovalError("请求指纹不匹配，文件可能已被篡改")
    return fingerprint


def _safe_int(value: object, default: int, minimum: int = 1, maximum: int = 86400) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _decision_summary(
    request: dict[str, Any],
    decision: dict[str, Any],
    *,
    idempotent: bool = False,
    superseded: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "request_id": request.get("id"),
        "decision": decision.get("decision"),
        "fingerprint": decision.get("fingerprint"),
        "summary": request.get("summary") or request.get("detail") or "",
        "target": request.get("target"),
        "operation": request.get("operation") or request.get("action"),
        "decided_by": decision.get("decided_by"),
        "decided_at": decision.get("decided_at"),
        "expires_at": decision.get("expires_at"),
        "idempotent": bool(idempotent),
        "superseded_duplicates": list(superseded or []),
        "approvals": len(decision.get("approvals") or []),
        "required_approvers": int(decision.get("required_approvers") or 1),
    }


def apply_owner_decision(
    request_id: object,
    action: str = "approve",
    reason: str = "",
    decided_by: str = "owner-cli",
    *,
    root: str | Path | None = None,
    at: datetime | None = None,
) -> dict[str, Any]:
    """Write one exact owner decision, preserving idempotency and quorum semantics."""

    rid = validate_request_id(request_id)
    normalized_action = str(action or "approve").strip().lower()
    if normalized_action not in _FINAL_DECISIONS:
        raise ApprovalError("action 只能是 approve 或 deny")
    actor = str(decided_by or "owner-cli").strip()
    if not _ACTOR_RE.fullmatch(actor):
        raise ApprovalError(f"非法批准人标识：{actor!r}")

    request = load_request(rid, root)
    fingerprint = request_fingerprint(request, rid)
    paths = approval_paths(root)
    decision_path = paths["decisions"] / f"{rid}.json"
    existing = read_json(decision_path)
    if existing and existing.get("decision") in _FINAL_DECISIONS:
        if (
            existing.get("decision") == normalized_action
            and hmac.compare_digest(str(existing.get("fingerprint", "")), fingerprint)
        ):
            return _decision_summary(request, existing, idempotent=True)
        raise ApprovalError(
            f"请求已被裁决为 {existing.get('decision')}，不允许覆盖为 {normalized_action}"
        )

    timestamp = at.astimezone(timezone.utc) if at else now()
    ttl_seconds = _safe_int(
        request.get("ttl_seconds"), _DEFAULT_DECISION_TTL, minimum=1, maximum=3600
    )
    clean_reason = _sanitize_text(reason)
    required_approvers = _safe_int(
        request.get("required_approvers"), 1, minimum=1, maximum=10
    )

    if normalized_action == "deny":
        decision: dict[str, Any] = {
            "schema_version": 1,
            "request_id": rid,
            "decision": "deny",
            "fingerprint": fingerprint,
            "decided_at": timestamp.isoformat(timespec="seconds"),
            "decided_by": actor,
            "expires_at": (timestamp + timedelta(seconds=ttl_seconds)).isoformat(
                timespec="seconds"
            ),
        }
        if clean_reason:
            decision["reason"] = clean_reason
        _atomic_json(decision_path, decision)
    elif required_approvers > 1:
        approvals = []
        if existing and existing.get("decision") == "collecting":
            approvals = [
                item
                for item in existing.get("approvals") or []
                if isinstance(item, dict) and item.get("decided_by")
            ]
        voters = {str(item.get("decided_by")) for item in approvals}
        if actor not in voters:
            approvals.append(
                {
                    "decided_by": actor,
                    "decided_at": timestamp.isoformat(timespec="seconds"),
                }
            )
            voters.add(actor)
        quorum = len(voters) >= required_approvers
        decision = {
            "schema_version": 2,
            "request_id": rid,
            "decision": "approve" if quorum else "collecting",
            "required_approvers": required_approvers,
            "approvals": approvals,
            "fingerprint": fingerprint,
            "decided_at": timestamp.isoformat(timespec="seconds"),
            "decided_by": actor,
            "expires_at": (timestamp + timedelta(seconds=ttl_seconds)).isoformat(
                timespec="seconds"
            ),
        }
        if request.get("second_confirmation_required"):
            decision["second_confirmation_required"] = True
        if clean_reason:
            decision["reason"] = clean_reason
        _atomic_json(decision_path, decision)
    else:
        decision = {
            "schema_version": 1,
            "request_id": rid,
            "decision": "approve",
            "fingerprint": fingerprint,
            "decided_at": timestamp.isoformat(timespec="seconds"),
            "decided_by": actor,
            "expires_at": (timestamp + timedelta(seconds=ttl_seconds)).isoformat(
                timespec="seconds"
            ),
        }
        if clean_reason:
            decision["reason"] = clean_reason
        try:
            _atomic_json(decision_path, decision, exclusive=True)
        except FileExistsError:
            raced = read_json(decision_path)
            if (
                raced
                and raced.get("decision") == "approve"
                and hmac.compare_digest(str(raced.get("fingerprint", "")), fingerprint)
            ):
                return _decision_summary(request, raced, idempotent=True)
            raise ApprovalError("请求在并发裁决时已被其他决定占用")

    _audit(root, normalized_action, f"request={rid} by={actor} reason={clean_reason}")
    return _decision_summary(request, decision)


def _operation_is_pending(request: dict[str, Any], root: str | Path | None = None) -> bool:
    rid = str(request.get("id", ""))
    paths = approval_paths(root)
    if (paths["operation_results"] / f"{rid}.json").is_file():
        return False
    decision = read_json(paths["decisions"] / f"{rid}.json")
    if decision and decision.get("decision") in _FINAL_DECISIONS:
        return False
    return str(request.get("risk", "")) != "read"


def list_pending_operations(root: str | Path | None = None) -> list[dict[str, Any]]:
    paths = approval_paths(root)
    rows: list[dict[str, Any]] = []
    directory = paths["ops_requests"]
    if not directory.is_dir():
        return rows
    for path in directory.glob("op-*.json"):
        request = read_json(path)
        if not request or not _operation_is_pending(request, root):
            continue
        try:
            fingerprint = request_fingerprint(request, str(request.get("id", "")))
        except ApprovalError:
            continue
        rows.append(
            {
                "id": request.get("id"),
                "fingerprint": fingerprint,
                "created_at": request.get("created_at", ""),
                "summary": _sanitize_text(request.get("summary", ""), 500),
                "target": request.get("target"),
                "operation": request.get("operation"),
                "risk": request.get("risk"),
                "mtime": path.stat().st_mtime,
            }
        )
    rows.sort(key=lambda item: (str(item.get("created_at", "")), float(item["mtime"])), reverse=True)
    for row in rows:
        row.pop("mtime", None)
    return rows


def resolve_pending_operation(
    request_id: str | None = None,
    root: str | Path | None = None,
) -> tuple[dict[str, Any], list[str]]:
    pending = list_pending_operations(root)
    normalized = str(request_id or "").strip().lower()
    if normalized in {"", "latest", "newest", "最新", "刚才", "该请求", "这个请求"}:
        if not pending:
            raise ApprovalError("当前没有等待主人审批的运维请求")
        if len(pending) == 1:
            return pending[0], []
        fingerprints = {str(item.get("fingerprint", "")) for item in pending}
        if len(fingerprints) == 1:
            selected = pending[0]
            duplicates = [str(item["id"]) for item in pending[1:]]
            return selected, duplicates
        raise AmbiguousApprovalError(
            "存在多个不同载荷的待审批请求，请明确输入请求 ID",
            pending=pending[:10],
        )
    rid = validate_request_id(normalized)
    request = load_request(rid, root)
    if not rid.startswith("op-"):
        return {
            "id": rid,
            "fingerprint": request_fingerprint(request, rid),
            "summary": _sanitize_text(request.get("detail", ""), 500),
            "target": request.get("target"),
            "operation": request.get("action"),
            "risk": request.get("risk"),
            "created_at": request.get("created_at", ""),
        }, []
    if not _operation_is_pending(request, root):
        decision = read_json(approval_paths(root)["decisions"] / f"{rid}.json")
        result = read_json(approval_paths(root)["operation_results"] / f"{rid}.json")
        state = "已执行" if result else f"已裁决为 {decision.get('decision')}" if decision else "无需审批"
        raise ApprovalError(f"请求 {rid} 当前不在等待审批状态：{state}")
    selected = next((item for item in pending if item.get("id") == rid), None)
    if selected is None:
        raise ApprovalError(f"请求 {rid} 不在待审批清单中")
    duplicates = [
        str(item["id"])
        for item in pending
        if item.get("id") != rid and item.get("fingerprint") == selected.get("fingerprint")
    ]
    return selected, duplicates


def _control_key_bytes(key: bytes | str | None = None) -> bytes:
    if isinstance(key, bytes):
        value = key
    elif isinstance(key, str) and key:
        value = key.encode("utf-8")
    else:
        path = Path(
            os.environ.get("AGENELF_APPROVAL_KEY_FILE", "/agenelf/approval/key")
        )
        try:
            value = path.read_bytes().strip()
        except OSError as exc:
            raise ApprovalError(f"审批控制密钥不可用：{path}: {exc}") from exc
    if len(value) < 32:
        raise ApprovalError("审批控制密钥长度不足 32 字节")
    return value


def _command_signature(payload: dict[str, Any], key: bytes) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(key, encoded, hashlib.sha256).hexdigest()


def submit_owner_command(
    request_id: object,
    action: str = "approve",
    reason: str = "",
    decided_by: str = "owner-cli",
    *,
    root: str | Path | None = None,
    key: bytes | str | None = None,
    ttl_seconds: int = _DEFAULT_COMMAND_TTL,
    supersede_duplicates: bool = True,
) -> dict[str, Any]:
    rid = validate_request_id(request_id)
    normalized_action = str(action or "approve").strip().lower()
    if normalized_action not in _FINAL_DECISIONS:
        raise ApprovalError("action 只能是 approve 或 deny")
    actor = str(decided_by or "owner-cli").strip()
    if not _ACTOR_RE.fullmatch(actor):
        raise ApprovalError(f"非法批准人标识：{actor!r}")
    request = load_request(rid, root)
    fingerprint = request_fingerprint(request, rid)
    issued = now()
    ttl = _safe_int(ttl_seconds, _DEFAULT_COMMAND_TTL, minimum=15, maximum=300)
    command_id = "owner-decision-" + uuid.uuid4().hex[:16]
    payload: dict[str, Any] = {
        "schema_version": 1,
        "id": command_id,
        "source": "interactive_cli",
        "request_id": rid,
        "request_fingerprint": fingerprint,
        "action": normalized_action,
        "reason": _sanitize_text(reason),
        "decided_by": actor,
        "supersede_duplicates": bool(supersede_duplicates),
        "issued_at": issued.isoformat(timespec="seconds"),
        "expires_at": (issued + timedelta(seconds=ttl)).isoformat(timespec="seconds"),
        "nonce": uuid.uuid4().hex,
    }
    payload["signature"] = _command_signature(payload, _control_key_bytes(key))
    path = approval_paths(root)["commands"] / f"{command_id}.json"
    _atomic_json(path, payload, exclusive=True)
    _audit(root, "owner_command_submitted", f"command={command_id} request={rid} action={normalized_action}")
    return {
        "id": command_id,
        "request_id": rid,
        "action": normalized_action,
        "expires_at": payload["expires_at"],
    }


def verify_owner_command(
    command: dict[str, Any],
    *,
    root: str | Path | None = None,
    key: bytes | str | None = None,
    at: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if command.get("schema_version") != 1:
        raise ApprovalError("不支持的 owner decision command 版本")
    command_id = str(command.get("id", ""))
    if not _COMMAND_ID_RE.fullmatch(command_id):
        raise ApprovalError("非法 owner decision command ID")
    if command.get("source") != "interactive_cli":
        raise ApprovalError("审批命令来源不是 interactive_cli")
    rid = validate_request_id(command.get("request_id"))
    action = str(command.get("action", "")).lower()
    if action not in _FINAL_DECISIONS:
        raise ApprovalError("审批命令 action 非法")
    actor = str(command.get("decided_by", ""))
    if not _ACTOR_RE.fullmatch(actor):
        raise ApprovalError("审批命令批准人标识非法")
    signature = str(command.get("signature", ""))
    unsigned = dict(command)
    unsigned.pop("signature", None)
    expected = _command_signature(unsigned, _control_key_bytes(key))
    if not hmac.compare_digest(signature, expected):
        raise ApprovalError("审批命令签名无效")
    try:
        expires_at = datetime.fromisoformat(str(command.get("expires_at", "")))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ApprovalError("审批命令 expires_at 非法") from exc
    current = (at or now()).astimezone(timezone.utc)
    if current > expires_at.astimezone(timezone.utc):
        raise ApprovalError("审批命令已过期")
    request = load_request(rid, root)
    current_fingerprint = request_fingerprint(request, rid)
    if not hmac.compare_digest(
        str(command.get("request_fingerprint", "")), current_fingerprint
    ):
        raise ApprovalError("审批命令绑定的请求指纹已变化")
    return command, request


def _deny_identical_pending(
    approved_request_id: str,
    fingerprint: str,
    actor: str,
    *,
    root: str | Path | None = None,
) -> list[str]:
    denied: list[str] = []
    for item in list_pending_operations(root):
        rid = str(item.get("id", ""))
        if rid == approved_request_id or item.get("fingerprint") != fingerprint:
            continue
        try:
            apply_owner_decision(
                rid,
                "deny",
                reason=f"superseded by approved duplicate {approved_request_id}",
                decided_by=actor,
                root=root,
            )
            denied.append(rid)
        except ApprovalError:
            continue
    return denied


def process_owner_command(
    command: dict[str, Any],
    *,
    root: str | Path | None = None,
    key: bytes | str | None = None,
) -> dict[str, Any]:
    verified, request = verify_owner_command(command, root=root, key=key)
    summary = apply_owner_decision(
        verified["request_id"],
        verified["action"],
        reason=str(verified.get("reason", "")),
        decided_by=str(verified["decided_by"]),
        root=root,
    )
    superseded: list[str] = []
    if (
        verified["action"] == "approve"
        and bool(verified.get("supersede_duplicates", True))
        and str(verified["request_id"]).startswith("op-")
    ):
        superseded = _deny_identical_pending(
            str(verified["request_id"]),
            request_fingerprint(request, str(verified["request_id"])),
            str(verified["decided_by"]),
            root=root,
        )
        summary["superseded_duplicates"] = superseded
    return {
        "schema_version": 1,
        "id": verified["id"],
        "status": "succeeded",
        "finished_at": now_iso(),
        "request_id": verified["request_id"],
        "decision": summary,
    }


def write_command_result(
    command_id: str,
    result: dict[str, Any],
    *,
    root: str | Path | None = None,
) -> None:
    if not _COMMAND_ID_RE.fullmatch(str(command_id)):
        raise ApprovalError("非法 owner decision command ID")
    path = approval_paths(root)["command_results"] / f"{command_id}.json"
    _atomic_json(path, result, exclusive=True)


def wait_for_command_result(
    command_id: str,
    *,
    root: str | Path | None = None,
    timeout_seconds: float = 8.0,
    poll_interval: float = 0.1,
) -> dict[str, Any]:
    if not _COMMAND_ID_RE.fullmatch(str(command_id)):
        raise ApprovalError("非法 owner decision command ID")
    path = approval_paths(root)["command_results"] / f"{command_id}.json"
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while True:
        result = read_json(path)
        if result is not None:
            return result
        if time.monotonic() >= deadline:
            return {
                "schema_version": 1,
                "id": command_id,
                "status": "timeout",
                "error": "approval-runner 未在等待时间内返回结果",
            }
        time.sleep(max(0.02, float(poll_interval)))


def default_actor() -> str:
    raw = (
        os.environ.get("AGENELF_OWNER_ID")
        or os.environ.get("USERNAME")
        or os.environ.get("USER")
        or "owner"
    )
    cleaned = re.sub(r"[^A-Za-z0-9._@:-]", "-", str(raw))[:64].strip("-")
    return f"cli:{cleaned or 'owner'}"


def parse_owner_decision(text: object) -> dict[str, str] | None:
    """Parse only explicit raw owner commands; ordinary prose is never approval."""

    value = str(text or "").strip()
    if not value:
        return None
    trimmed = value.rstrip("。！! ")
    lower = trimmed.lower()
    slash_action = None
    if lower.startswith("/approve"):
        slash_action = "approve"
        remainder = trimmed[len("/approve") :].strip()
    elif lower.startswith("/deny"):
        slash_action = "deny"
        remainder = trimmed[len("/deny") :].strip()
    else:
        approve_prefixes = ("审批通过", "批准通过", "批准", "同意执行", "确认执行")
        deny_prefixes = ("审批拒绝", "拒绝执行", "不批准", "驳回", "拒绝")
        matched = next((prefix for prefix in approve_prefixes if trimmed == prefix or trimmed.startswith(prefix + " ")), None)
        if matched:
            slash_action = "approve"
            remainder = trimmed[len(matched) :].strip()
        else:
            matched = next((prefix for prefix in deny_prefixes if trimmed == prefix or trimmed.startswith(prefix + " ")), None)
            if not matched:
                return None
            slash_action = "deny"
            remainder = trimmed[len(matched) :].strip()
    remainder = re.sub(r"^(?:请求|该请求|这个请求|刚才的请求)\s*", "", remainder)
    match = _REQUEST_ID_RE.search(remainder.lower())
    request_id = match.group(0) if match else ""
    if match:
        remainder = (remainder[: match.start()] + " " + remainder[match.end() :]).strip()
    remainder = re.sub(r"^(?:latest|newest|最新|刚才|该请求|这个请求)\s*", "", remainder, flags=re.I)
    remainder = re.sub(r"^(?:原因|因为|备注)\s*[:：]?\s*", "", remainder)
    return {
        "action": slash_action,
        "request_id": request_id,
        "reason": _sanitize_text(remainder),
    }
