"""Persistent restart-safe task continuation checkpoints.

When the owner asks Agenelf to improve/reload a skill and then continue the original
job, the Agent must checkpoint the bounded resume prompt before entering the
self-evolution or restart path.  ``app/resume.py`` claims one pending checkpoint on
the next CLI start and performs a single idempotent resume attempt.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.privacy import redact_sensitive_text

SKILL_META = {
    "name": "task_continuation",
    "description": (
        "重启安全的任务续跑检查点。当主人要求升级/重载技能后继续当前任务时，"
        "必须在 autonomy/evolution/restart 前先保存检查点；下次 make chat 会自动续跑一次。"
    ),
    "version": "1.0.0",
}

CAPABILITY_META = {
    "id": "agent.task_continuation",
    "name": "任务续跑检查点",
    "description": (
        "把主人已明确要求继续的任务保存为有界、脱敏、带过期和幂等键的本地状态；"
        "不保存凭据，也不绕过远程操作审批。"
    ),
    "version": "1.0.0",
    "domain": "orchestration",
    "operations": [
        {"name": "checkpoint_task_continuation", "description": "保存重启后续跑任务", "risk": "change"},
        {"name": "task_continuation_status", "description": "查看续跑状态", "risk": "read"},
        {"name": "complete_task_continuation", "description": "标记续跑任务完成", "risk": "change"},
        {"name": "retry_task_continuation", "description": "重新排队一次未完成续跑", "risk": "change"},
        {"name": "cancel_task_continuation", "description": "取消续跑任务", "risk": "change"},
    ],
    "composes_with": [
        "agent.self_development",
        "agent.self_reflection",
        "agent.evolution",
        "docker.operations",
        "server.operations",
    ],
}

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "checkpoint_task_continuation",
            "description": (
                "当主人要求‘完善/升级/重载技能，然后继续当前任务’时，在调用 autonomy、"
                "evolution 或触发重启前必须先调用本工具。保存脱敏后的任务摘要和续跑提示；"
                "不代表外部变更已获批准。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_summary": {"type": "string"},
                    "resume_prompt": {"type": "string"},
                    "reason": {"type": "string"},
                    "expires_minutes": {"type": "integer", "minimum": 5, "maximum": 10080},
                    "max_attempts": {"type": "integer", "minimum": 1, "maximum": 3},
                },
                "required": ["task_summary", "resume_prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_continuation_status",
            "description": "查看当前续跑检查点的状态、次数和摘要；不返回完整续跑提示。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_task_continuation",
            "description": "原任务已真实完成并有工具证据后，按 continuation_id 标记完成。",
            "parameters": {
                "type": "object",
                "properties": {
                    "continuation_id": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["continuation_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "retry_task_continuation",
            "description": "把 attempted/failed 且仍有剩余次数的续跑检查点重新置为 pending。",
            "parameters": {
                "type": "object",
                "properties": {"continuation_id": {"type": "string"}},
                "required": ["continuation_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_task_continuation",
            "description": "主人撤销或任务范围改变时取消续跑检查点。",
            "parameters": {
                "type": "object",
                "properties": {
                    "continuation_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["continuation_id"],
            },
        },
    },
]

_ID_RE = re.compile(r"resume-[A-Za-z0-9._-]+")
_PROXY_URI_RE = re.compile(
    r"(?i)\b(vmess|vless|trojan|ss|ssr|hysteria2?|tuic)://[^\s\"']+"
)
_MAX_SUMMARY = 1000
_MAX_PROMPT = 5000
_MAX_REASON = 1000
_MAX_RESULT = 2500


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _root() -> Path:
    configured = os.environ.get("AGENELF_ROOT", "").strip()
    return Path(configured).resolve() if configured else Path(__file__).resolve().parents[2]


def _store_dir() -> Path:
    path = _root() / "data" / "continuations"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _state_path() -> Path:
    return _store_dir() / "pending.json"


def _history_dir() -> Path:
    path = _store_dir() / "history"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sanitize(value: Any, limit: int) -> str:
    text = redact_sensitive_text(value)
    text = _PROXY_URI_RE.sub(lambda match: f"{match.group(1)}://[REDACTED]", text)
    text = " ".join(text.strip().split())
    return text[:limit]


def _read(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=".tmp", dir=path.parent, text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _archive(state: dict[str, Any]) -> None:
    continuation_id = str(state.get("id", "unknown"))
    if not _ID_RE.fullmatch(continuation_id):
        continuation_id = "resume-invalid-" + uuid.uuid4().hex[:8]
    _write(_history_dir() / f"{continuation_id}.json", state)


def _load_state() -> dict[str, Any] | None:
    return _read(_state_path())


def _save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = _now_iso()
    _write(_state_path(), state)


def _expired(state: dict[str, Any]) -> bool:
    try:
        value = datetime.fromisoformat(str(state.get("expires_at", "")))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return _now() > value
    except ValueError:
        return True


def _public(state: dict[str, Any] | None) -> dict[str, Any]:
    if state is None:
        return {"exists": False, "status": "none"}
    return {
        "exists": True,
        "id": state.get("id"),
        "status": state.get("status"),
        "task_summary": state.get("task_summary"),
        "reason": state.get("reason"),
        "attempt_count": state.get("attempt_count", 0),
        "max_attempts": state.get("max_attempts", 1),
        "created_at": state.get("created_at"),
        "updated_at": state.get("updated_at"),
        "expires_at": state.get("expires_at"),
        "evidence": state.get("evidence", []),
        "last_result": state.get("last_result", ""),
        "last_error": state.get("last_error", ""),
    }


def checkpoint(
    task_summary: str,
    resume_prompt: str,
    reason: str = "skill_upgrade_or_restart",
    expires_minutes: int = 1440,
    max_attempts: int = 2,
) -> dict[str, Any]:
    summary = _sanitize(task_summary, _MAX_SUMMARY)
    prompt = _sanitize(resume_prompt, _MAX_PROMPT)
    clean_reason = _sanitize(reason, _MAX_REASON) or "skill_upgrade_or_restart"
    if not summary or not prompt:
        raise ValueError("task_summary 与 resume_prompt 不能为空")
    expires_minutes = max(5, min(int(expires_minutes), 10080))
    max_attempts = max(1, min(int(max_attempts), 3))
    key_source = json.dumps(
        {"task_summary": summary, "resume_prompt": prompt},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    idempotency_key = hashlib.sha256(key_source.encode("utf-8")).hexdigest()
    current = _load_state()
    if current and current.get("idempotency_key") == idempotency_key:
        status = str(current.get("status", ""))
        if status in {"pending", "running"}:
            return _public(current)
        if status in {"attempted", "failed"} and int(current.get("attempt_count", 0)) < int(
            current.get("max_attempts", 1)
        ):
            current["status"] = "pending"
            current["last_error"] = ""
            _save_state(current)
            return _public(current)
    if current:
        _archive(current)
    now = _now()
    state = {
        "schema_version": 1,
        "id": "resume-" + now.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6],
        "status": "pending",
        "task_summary": summary,
        "resume_prompt": prompt,
        "reason": clean_reason,
        "idempotency_key": idempotency_key,
        "attempt_count": 0,
        "max_attempts": max_attempts,
        "created_at": now.isoformat(timespec="seconds"),
        "updated_at": now.isoformat(timespec="seconds"),
        "expires_at": (now + timedelta(minutes=expires_minutes)).isoformat(timespec="seconds"),
        "evidence": [],
        "last_result": "",
        "last_error": "",
    }
    _save_state(state)
    return _public(state)


def claim_pending() -> dict[str, Any] | None:
    """Claim exactly one pending checkpoint for the resume entrypoint."""

    state = _load_state()
    if state is None or state.get("status") != "pending":
        return None
    if _expired(state):
        state["status"] = "expired"
        state["last_error"] = "continuation expired before resume"
        _save_state(state)
        return None
    attempts = int(state.get("attempt_count", 0))
    maximum = int(state.get("max_attempts", 1))
    if attempts >= maximum:
        state["status"] = "failed"
        state["last_error"] = "maximum resume attempts reached"
        _save_state(state)
        return None
    state["status"] = "running"
    state["attempt_count"] = attempts + 1
    state["claimed_at"] = _now_iso()
    _save_state(state)
    return dict(state)


def finish_attempt(continuation_id: str, result: str = "", error: str = "") -> dict[str, Any]:
    state = _load_state()
    if state is None or state.get("id") != continuation_id:
        raise ValueError("continuation 不存在或 ID 不匹配")
    if state.get("status") in {"completed", "cancelled"}:
        return _public(state)
    state["last_result"] = _sanitize(result, _MAX_RESULT)
    state["last_error"] = _sanitize(error, _MAX_RESULT)
    if error:
        if int(state.get("attempt_count", 0)) < int(state.get("max_attempts", 1)):
            state["status"] = "pending"
        else:
            state["status"] = "failed"
    else:
        state["status"] = "attempted"
    _save_state(state)
    return _public(state)


def status() -> dict[str, Any]:
    state = _load_state()
    if state and state.get("status") in {"pending", "running"} and _expired(state):
        state["status"] = "expired"
        state["last_error"] = "continuation expired"
        _save_state(state)
    return _public(state)


def complete(continuation_id: str, evidence: list[Any] | None = None) -> dict[str, Any]:
    state = _load_state()
    if state is None or state.get("id") != continuation_id:
        raise ValueError("continuation 不存在或 ID 不匹配")
    values = []
    for item in evidence or []:
        text = _sanitize(item, 500)
        if text:
            values.append(text)
    state["status"] = "completed"
    state["completed_at"] = _now_iso()
    state["evidence"] = values[:20]
    _save_state(state)
    _archive(state)
    return _public(state)


def retry(continuation_id: str) -> dict[str, Any]:
    state = _load_state()
    if state is None or state.get("id") != continuation_id:
        raise ValueError("continuation 不存在或 ID 不匹配")
    if state.get("status") in {"completed", "cancelled", "expired"}:
        raise ValueError(f"状态 {state.get('status')} 不允许重试")
    if int(state.get("attempt_count", 0)) >= int(state.get("max_attempts", 1)):
        raise ValueError("续跑次数已经耗尽")
    state["status"] = "pending"
    state["last_error"] = ""
    _save_state(state)
    return _public(state)


def cancel(continuation_id: str, reason: str = "") -> dict[str, Any]:
    state = _load_state()
    if state is None or state.get("id") != continuation_id:
        raise ValueError("continuation 不存在或 ID 不匹配")
    state["status"] = "cancelled"
    state["cancelled_at"] = _now_iso()
    state["cancel_reason"] = _sanitize(reason, _MAX_REASON)
    _save_state(state)
    _archive(state)
    return _public(state)


def execute(tool_name: str, args: dict[str, Any]) -> str:
    data = args or {}
    try:
        if tool_name == "checkpoint_task_continuation":
            value = checkpoint(
                str(data.get("task_summary", "")),
                str(data.get("resume_prompt", "")),
                str(data.get("reason", "skill_upgrade_or_restart")),
                int(data.get("expires_minutes", 1440) or 1440),
                int(data.get("max_attempts", 2) or 2),
            )
        elif tool_name == "task_continuation_status":
            value = status()
        elif tool_name == "complete_task_continuation":
            value = complete(
                str(data.get("continuation_id", "")), data.get("evidence", [])
            )
        elif tool_name == "retry_task_continuation":
            value = retry(str(data.get("continuation_id", "")))
        elif tool_name == "cancel_task_continuation":
            value = cancel(
                str(data.get("continuation_id", "")), str(data.get("reason", ""))
            )
        else:
            return json.dumps({"ok": False, "error": f"未知工具：{tool_name}"}, ensure_ascii=False)
        return json.dumps({"ok": True, "continuation": value}, ensure_ascii=False, indent=2)
    except (TypeError, ValueError, OSError) as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
            ensure_ascii=False,
            indent=2,
        )
