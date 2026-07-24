"""File-backed queue for deterministic software validation.

The LLM-facing Agent may select only owner-configured validation aliases.  It never
receives a free-form URL or host from the model.  A separate validation runner owns
network execution and writes trusted result files that are read-only in the Agent
container.
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

_CAPABILITY = "software.validation"
_ID_RE = re.compile(r"val-[0-9a-f]{16}")
_VALID_OPERATIONS = {"run_check", "run_suite"}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def runtime_root() -> Path:
    configured = os.environ.get("AGENELF_ROOT", "").strip()
    return Path(configured).resolve() if configured else Path(__file__).resolve().parents[2]


def queue_paths(root: Path | None = None) -> dict[str, Path]:
    base = (root or runtime_root()).resolve()
    data = base / "data"
    return {
        "requests": data / "validation-requests",
        "results": data / "validation-results",
        "locks": data / "validation-locks",
        "audit": base / "logs" / "validation.log",
    }


def canonical_payload(operation: str, target: str) -> dict[str, Any]:
    operation = str(operation or "").strip()
    target = str(target or "").strip()
    if operation not in _VALID_OPERATIONS:
        raise ValueError(f"不支持的验证操作：{operation!r}")
    if not target:
        raise ValueError("验证目标不能为空")
    return {
        "capability": _CAPABILITY,
        "operation": operation,
        "target": target,
        "parameters": {},
    }


def payload_fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_id(validation_id: str) -> str:
    value = str(validation_id or "").strip()
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"非法验证 ID：{validation_id!r}")
    return value


def _atomic_json(path: Path, value: dict[str, Any], *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if exclusive:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
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
        pass


def submit_validation(
    operation: str,
    target: str,
    summary: str,
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    payload = canonical_payload(operation, target)
    validation_id = f"val-{uuid.uuid4().hex[:16]}"
    request = {
        "schema_version": 1,
        "id": validation_id,
        **payload,
        "risk": "read",
        "summary": str(summary or "").strip(),
        "fingerprint": payload_fingerprint(payload),
        "created_at": now_iso(),
        "created_by": "agenelf-agent",
    }
    path = queue_paths(root)["requests"] / f"{validation_id}.json"
    _atomic_json(path, request, exclusive=True)
    audit("validation_submitted", f"{validation_id} {operation} target={target}", root)
    return request


def get_validation(validation_id: str, *, root: Path | None = None) -> dict[str, Any]:
    validation_id = _validate_id(validation_id)
    paths = queue_paths(root)
    request = read_json(paths["requests"] / f"{validation_id}.json")
    if request is None:
        return {"id": validation_id, "status": "not_found"}
    result = read_json(paths["results"] / f"{validation_id}.json")
    if result is not None:
        return {
            "id": validation_id,
            "status": str(result.get("status", "finished")),
            "request": request,
            "result": result,
        }
    return {"id": validation_id, "status": "queued", "request": request}


def wait_for_validation(
    validation_id: str,
    timeout_seconds: float = 0,
    poll_interval: float = 0.2,
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while True:
        state = get_validation(validation_id, root=root)
        if state.get("result") is not None or state.get("status") in {
            "failed",
            "blocked",
            "not_found",
        }:
            return state
        if time.monotonic() >= deadline:
            return state
        time.sleep(max(0.05, float(poll_interval)))
