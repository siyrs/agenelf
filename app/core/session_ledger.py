"""Append-only, branchable session event ledger.

The ledger borrows Pi's tree-shaped session model while preserving Agenelf's
owner-local privacy and governance boundaries:

- entries are appended to one JSONL file per session;
- ``parent_id`` forms a conversation/event tree;
- ``prev_hash`` + ``entry_hash`` form an append-order hash chain;
- payloads are recursively redacted before persistence;
- the store has no tool execution capability and never reads owner secrets.

The JSON shape is intentionally language-neutral so the future Node.js runtime
can adopt the same records without a second data migration.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any

from .privacy import sanitize_value
from .storage import now_iso

SCHEMA_VERSION = 1
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_LEDGER_BYTES = 32 * 1024 * 1024
DEFAULT_LIMIT = 50
MAX_LIMIT = 500

ENTRY_TYPES = {
    "message",
    "tool_call",
    "tool_result",
    "checkpoint",
    "reflection",
    "intention",
    "approval_ref",
    "evidence_ref",
    "branch_summary",
    "compaction",
    "label",
    "custom",
}

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ENTRY_ID_RE = re.compile(r"^evt-[0-9a-f]{16}$")
_BRANCH_ID_RE = re.compile(r"^(?:main|br-[0-9a-f]{12})$")

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class SessionLedgerError(ValueError):
    """Raised when a session ledger request or persisted record is invalid."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _entry_hash(entry_without_hash: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(entry_without_hash)).hexdigest()


def _session_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


def _safe_session_id(value: object) -> str:
    session_id = str(value or "").strip()
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise SessionLedgerError(
            "session_id 只能包含字母、数字、点、下划线、连字符，"
            "以字母或数字开头，长度 1-64"
        )
    return session_id


def _safe_entry_id(value: object, *, optional: bool = False) -> str | None:
    text = str(value or "").strip()
    if optional and not text:
        return None
    if not _ENTRY_ID_RE.fullmatch(text):
        raise SessionLedgerError(f"非法 ledger entry id：{text!r}")
    return text


def _safe_branch_id(value: object, *, optional: bool = False) -> str | None:
    text = str(value or "").strip()
    if optional and not text:
        return None
    if not _BRANCH_ID_RE.fullmatch(text):
        raise SessionLedgerError(f"非法 branch_id：{text!r}")
    return text


def _safe_event_type(value: object) -> str:
    event_type = str(value or "").strip()
    if event_type not in ENTRY_TYPES:
        allowed = "、".join(sorted(ENTRY_TYPES))
        raise SessionLedgerError(f"event_type 必须是：{allowed}")
    return event_type


def _safe_payload(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SessionLedgerError("payload 必须是 JSON object")
    warnings: list[str] = []
    safe = sanitize_value(value, path="payload", warnings=warnings, max_depth=8)
    if not isinstance(safe, dict):
        raise SessionLedgerError("payload 清洗后不是 object")
    if warnings:
        safe = {**safe, "_privacy_warnings": warnings[:20]}
    encoded = _canonical_bytes(safe)
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise SessionLedgerError(
            f"payload 超过 {MAX_PAYLOAD_BYTES} 字节上限，拒绝写入 session ledger"
        )
    return safe


class SessionLedgerStore:
    """Owner-local append-only session ledger with tree and hash-chain semantics."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.ledger_dir = self.root / "local" / "memory" / "session-ledger"

    def _path(self, session_id: str) -> Path:
        return self.ledger_dir / f"{_safe_session_id(session_id)}.jsonl"

    @staticmethod
    def _read_lines(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise SessionLedgerError("无法读取 session ledger 元数据") from exc
        if size > MAX_LEDGER_BYTES:
            raise SessionLedgerError(
                f"session ledger 超过 {MAX_LEDGER_BYTES} 字节读取上限"
            )
        entries: list[dict[str, Any]] = []
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line_no, raw in enumerate(handle, start=1):
                    text = raw.strip()
                    if not text:
                        continue
                    try:
                        value = json.loads(text)
                    except json.JSONDecodeError as exc:
                        raise SessionLedgerError(
                            f"session ledger 第 {line_no} 行不是有效 JSON"
                        ) from exc
                    if not isinstance(value, dict):
                        raise SessionLedgerError(
                            f"session ledger 第 {line_no} 行必须是 object"
                        )
                    entries.append(value)
        except OSError as exc:
            raise SessionLedgerError("读取 session ledger 失败") from exc
        return entries

    @staticmethod
    def _write_line(path: Path, entry: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = _canonical_bytes(entry) + b"\n"
        try:
            current_size = path.stat().st_size if path.exists() else 0
        except OSError as exc:
            raise SessionLedgerError("无法读取 session ledger 写入前大小") from exc
        if current_size + len(data) > MAX_LEDGER_BYTES:
            raise SessionLedgerError(
                f"session ledger 写入后将超过 {MAX_LEDGER_BYTES} 字节上限"
            )
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            offset = 0
            while offset < len(data):
                written = os.write(fd, data[offset:])
                if written <= 0:
                    raise OSError("session ledger append returned zero bytes")
                offset += written
            os.fsync(fd)
        finally:
            os.close(fd)

    def append(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        parent_id: str | None = None,
        branch_id: str | None = None,
    ) -> dict[str, Any]:
        session_id = _safe_session_id(session_id)
        event_type = _safe_event_type(event_type)
        safe_payload = _safe_payload(payload)
        requested_parent = _safe_entry_id(parent_id, optional=True)
        requested_branch = _safe_branch_id(branch_id, optional=True)
        path = self._path(session_id)

        with _session_lock(path):
            entries = self._read_lines(path)
            by_id = {
                str(item.get("id")): item
                for item in entries
                if isinstance(item.get("id"), str)
            }
            if requested_parent is not None and requested_parent not in by_id:
                raise SessionLedgerError(
                    f"parent_id 不存在于 session {session_id}：{requested_parent}"
                )

            previous = entries[-1] if entries else None
            parent = requested_parent
            if parent is None and previous is not None:
                parent = str(previous.get("id") or "") or None

            if requested_branch is not None:
                branch = requested_branch
            elif parent is not None and parent in by_id:
                branch = str(by_id[parent].get("branch_id") or "main")
            else:
                branch = "main"
            _safe_branch_id(branch)

            entry_without_hash: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "id": "evt-" + uuid.uuid4().hex[:16],
                "session_id": session_id,
                "seq": len(entries) + 1,
                "parent_id": parent,
                "branch_id": branch,
                "type": event_type,
                "ts": now_iso(),
                "payload": safe_payload,
                "prev_hash": str(previous.get("entry_hash") or "")
                if previous is not None
                else "",
            }
            entry = {
                **entry_without_hash,
                "entry_hash": _entry_hash(entry_without_hash),
            }
            self._write_line(path, entry)
            return entry

    def create_branch(
        self,
        session_id: str,
        parent_id: str,
        *,
        label: str,
        summary: str = "",
    ) -> dict[str, Any]:
        parent = _safe_entry_id(parent_id)
        safe_label = str(label or "").strip()
        if not safe_label:
            raise SessionLedgerError("branch label 不能为空")
        branch_id = "br-" + uuid.uuid4().hex[:12]
        return self.append(
            session_id,
            "branch_summary",
            {
                "label": safe_label[:200],
                "summary": str(summary or "").strip()[:4000],
                "branched_from": parent,
            },
            parent_id=parent,
            branch_id=branch_id,
        )

    def entries(
        self,
        session_id: str,
        *,
        limit: int = DEFAULT_LIMIT,
        event_type: str = "",
        branch_id: str = "",
    ) -> list[dict[str, Any]]:
        session_id = _safe_session_id(session_id)
        if event_type:
            event_type = _safe_event_type(event_type)
        if branch_id:
            branch_id = str(_safe_branch_id(branch_id))
        try:
            bounded_limit = max(0, min(int(limit), MAX_LIMIT))
        except (TypeError, ValueError):
            bounded_limit = DEFAULT_LIMIT
        path = self._path(session_id)
        with _session_lock(path):
            values = self._read_lines(path)
        if event_type:
            values = [item for item in values if item.get("type") == event_type]
        if branch_id:
            values = [item for item in values if item.get("branch_id") == branch_id]
        return values[-bounded_limit:] if bounded_limit else []

    def get(self, session_id: str, entry_id: str) -> dict[str, Any]:
        session_id = _safe_session_id(session_id)
        entry_id = str(_safe_entry_id(entry_id))
        path = self._path(session_id)
        with _session_lock(path):
            values = self._read_lines(path)
        for entry in values:
            if entry.get("id") == entry_id:
                return entry
        raise SessionLedgerError(f"session ledger entry 不存在：{entry_id}")

    def verify(self, session_id: str) -> dict[str, Any]:
        session_id = _safe_session_id(session_id)
        path = self._path(session_id)
        with _session_lock(path):
            entries = self._read_lines(path)

        seen: set[str] = set()
        previous_hash = ""
        errors: list[str] = []
        branches: set[str] = set()
        for index, entry in enumerate(entries, start=1):
            entry_id = str(entry.get("id") or "")
            raw_parent_id = entry.get("parent_id")
            parent_id = raw_parent_id if isinstance(raw_parent_id, str) else None
            branch_id = str(entry.get("branch_id") or "")
            event_type = str(entry.get("type") or "")
            if entry.get("schema_version") != SCHEMA_VERSION:
                errors.append(f"seq={index}: schema_version 非 {SCHEMA_VERSION}")
            if entry.get("session_id") != session_id:
                errors.append(f"seq={index}: session_id 不匹配")
            if entry.get("seq") != index:
                errors.append(f"seq={index}: 持久化 seq={entry.get('seq')!r}")
            if not _ENTRY_ID_RE.fullmatch(entry_id) or entry_id in seen:
                errors.append(f"seq={index}: entry id 非法或重复")
            if raw_parent_id is not None:
                if not isinstance(raw_parent_id, str) or not _ENTRY_ID_RE.fullmatch(raw_parent_id):
                    errors.append(f"seq={index}: parent_id 格式非法")
                elif parent_id not in seen:
                    errors.append(f"seq={index}: parent_id 未指向先前 entry")
            if not _BRANCH_ID_RE.fullmatch(branch_id):
                errors.append(f"seq={index}: branch_id 非法")
            else:
                branches.add(branch_id)
            if event_type not in ENTRY_TYPES:
                errors.append(f"seq={index}: event type 未注册")
            if entry.get("prev_hash") != previous_hash:
                errors.append(f"seq={index}: prev_hash 链断裂")

            candidate = dict(entry)
            stored_hash = str(candidate.pop("entry_hash", ""))
            computed = _entry_hash(candidate)
            if stored_hash != computed:
                errors.append(f"seq={index}: entry_hash 校验失败")
            previous_hash = stored_hash
            seen.add(entry_id)

        return {
            "schema_version": SCHEMA_VERSION,
            "session_id": session_id,
            "entries": len(entries),
            "branches": sorted(branches),
            "last_entry_id": entries[-1].get("id") if entries else None,
            "last_hash": previous_hash,
            "integrity": "ok" if not errors else "failed",
            "errors": errors[:50],
        }

    def status(self, session_id: str) -> dict[str, Any]:
        verification = self.verify(session_id)
        verification["storage"] = (
            f"local/memory/session-ledger/{_safe_session_id(session_id)}.jsonl"
        )
        return verification
