"""Persistent, secret-safe state for task continuation.

This module owns checkpoint files and process-recovery leases.  It contains no LLM
or tool-dispatch logic, which keeps the at-most-once state machine independently
testable and reusable by CLI/HTTP runtimes.
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

from .privacy import redact_sensitive_text

CHECKPOINT_SCHEMA = 2
MAX_CHECKPOINTS = 50
MAX_RESUME_ATTEMPTS = 3
LEASE_SECONDS = 300
SAFE_ID = re.compile(r"[A-Za-z0-9._-]+")
CONTINUATION_RE = re.compile(r"cont-[0-9a-f]{16}")


def now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def runtime_root() -> Path:
    configured = os.environ.get("AGENELF_ROOT", "").strip()
    return Path(configured).resolve() if configured else Path(__file__).resolve().parents[2]


def store_dir(root: Path) -> Path:
    path = root / "data" / "task-continuations"
    path.mkdir(parents=True, exist_ok=True)
    return path


def runtime_state_path(root: Path) -> Path:
    return root / "data" / "skill-runtime.json"


def active_path(root: Path) -> Path:
    return store_dir(root) / "active.json"


def checkpoint_path(root: Path, checkpoint_id: str) -> Path:
    value = str(checkpoint_id or "")
    if not CONTINUATION_RE.fullmatch(value):
        raise ValueError(f"非法续跑检查点 ID：{checkpoint_id!r}")
    return store_dir(root) / f"{value}.json"


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def atomic_json(path: Path, value: dict[str, Any], *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if exclusive:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        return
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=".tmp", dir=path.parent, text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def safe_text(value: object, limit: int = 4000) -> str:
    text = redact_sensitive_text(value)
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def sha256_text(value: object) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8", errors="replace")).hexdigest()


def parse_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def add_event(checkpoint: dict[str, Any], event: str, detail: object = "") -> None:
    checkpoint.setdefault("events", []).append(
        {"at": now_iso(), "event": event, "detail": safe_text(detail, 800)}
    )
    checkpoint["events"] = checkpoint["events"][-100:]
    checkpoint["updated_at"] = now_iso()


def write_checkpoint(root: Path, checkpoint: dict[str, Any]) -> None:
    checkpoint["updated_at"] = now_iso()
    atomic_json(checkpoint_path(root, str(checkpoint["id"])), checkpoint)


def load_checkpoint(root: Path, checkpoint_id: str) -> dict[str, Any] | None:
    try:
        return read_json(checkpoint_path(root, checkpoint_id))
    except ValueError:
        return None


def set_active(root: Path, checkpoint: dict[str, Any]) -> None:
    atomic_json(
        active_path(root),
        {
            "schema_version": 1,
            "checkpoint_id": checkpoint["id"],
            "status": checkpoint.get("status"),
            "updated_at": now_iso(),
        },
    )


def clear_active(root: Path, checkpoint_id: str) -> None:
    active = read_json(active_path(root))
    if active and active.get("checkpoint_id") != checkpoint_id:
        return
    try:
        active_path(root).unlink()
    except OSError:
        pass


def active_checkpoint(root: Path) -> dict[str, Any] | None:
    active = read_json(active_path(root))
    if not active:
        return None
    checkpoint = load_checkpoint(root, str(active.get("checkpoint_id", "")))
    if not checkpoint or checkpoint.get("status") not in {"running", "pending"}:
        return None
    return checkpoint


def prune(root: Path) -> None:
    paths = sorted(
        store_dir(root).glob("cont-*.json"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for path in paths[MAX_CHECKPOINTS:]:
        value = read_json(path)
        if value and value.get("status") in {"completed", "blocked", "cancelled"}:
            try:
                path.unlink()
            except OSError:
                pass


def new_checkpoint(
    *,
    agent: Any,
    goal: str,
    subject: str,
    root: Path,
    authorization: dict[str, Any],
    initial_scope: dict[str, Any],
) -> dict[str, Any]:
    generation = int(
        getattr(getattr(agent, "registry", None), "runtime_generation", 0) or 0
    )
    recent_context: list[str] = []
    for message in getattr(agent, "history", [])[-12:]:
        if isinstance(message, dict) and message.get("role") == "user":
            text = safe_text(message.get("content", ""), 1200)
            if text:
                recent_context.append(text)
    checkpoint = {
        "schema_version": CHECKPOINT_SCHEMA,
        "id": f"cont-{uuid.uuid4().hex[:16]}",
        "status": "running",
        "subject": safe_text(subject, 80),
        "original_goal": safe_text(goal),
        "original_goal_sha256": sha256_text(goal),
        "recent_user_context": recent_context[-4:],
        "authorization": authorization,
        "scope": dict(initial_scope),
        "skill_generation_at_start": generation,
        "skill_generation_current": generation,
        "attempts": 0,
        "max_attempts": MAX_RESUME_ATTEMPTS,
        "runtime_instance": str(
            getattr(agent, "_task_continuation_instance", "")
        ),
        "owner_pid": os.getpid(),
        "lease_until": (now() + timedelta(seconds=LEASE_SECONDS)).isoformat(
            timespec="seconds"
        ),
        "last_reply": "",
        "last_error": "",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "events": [],
    }
    add_event(checkpoint, "created", "对话开始前已建立可恢复检查点")
    write_checkpoint(root, checkpoint)
    set_active(root, checkpoint)
    prune(root)
    return checkpoint


def lock_path(root: Path, checkpoint_id: str) -> Path:
    return store_dir(root) / f"{checkpoint_id}.lock"


def acquire_lock(root: Path, checkpoint: dict[str, Any], instance: str) -> bool:
    path = lock_path(root, str(checkpoint["id"]))
    value = {
        "checkpoint_id": checkpoint["id"],
        "runtime_instance": instance,
        "pid": os.getpid(),
        "expires_at": (now() + timedelta(seconds=LEASE_SECONDS)).isoformat(
            timespec="seconds"
        ),
    }
    for _ in range(2):
        try:
            atomic_json(path, value, exclusive=True)
            return True
        except FileExistsError:
            current = read_json(path) or {}
            expires = parse_time(current.get("expires_at"))
            # A different instance is not proof that the previous owner died.
            # Only an expired lease may be reclaimed.
            if expires and expires <= now():
                try:
                    path.unlink()
                except OSError:
                    return False
                continue
            return False
    return False


def release_lock(root: Path, checkpoint_id: str, instance: str) -> None:
    path = lock_path(root, checkpoint_id)
    current = read_json(path)
    if current and current.get("runtime_instance") not in {None, "", instance}:
        return
    try:
        path.unlink()
    except OSError:
        pass


def find_resumable(root: Path) -> dict[str, Any] | None:
    active = active_checkpoint(root)
    if active:
        return active
    paths = sorted(
        store_dir(root).glob("cont-*.json"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for path in paths:
        value = read_json(path)
        if value and value.get("status") in {"running", "pending"}:
            return value
    return None


def status_snapshot(root: Path) -> dict[str, Any]:
    runtime = read_json(runtime_state_path(root)) or {}
    active = read_json(active_path(root)) or {}
    latest: list[dict[str, Any]] = []
    for path in sorted(
        store_dir(root).glob("cont-*.json"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )[:10]:
        value = read_json(path)
        if not value:
            continue
        latest.append(
            {
                "id": value.get("id"),
                "status": value.get("status"),
                "original_goal": value.get("original_goal"),
                "attempts": value.get("attempts"),
                "skill_generation_current": value.get(
                    "skill_generation_current"
                ),
                "scope": value.get("scope", {}),
                "updated_at": value.get("updated_at"),
                "last_error": value.get("last_error", ""),
            }
        )
    return {
        "runtime_generation": runtime.get("generation", 0),
        "runtime_fingerprint": runtime.get("fingerprint", ""),
        "active": active,
        "checkpoints": latest,
    }
