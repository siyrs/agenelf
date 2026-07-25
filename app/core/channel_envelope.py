"""Channel-neutral command envelopes for CLI, HTTP, Web, Mobile and Voice.

Every client normalizes into the same persisted envelope before task planning.  The
store provides actor/session-scoped idempotency and rejects a reused key when the
payload changes.  Credential-like text is redacted before persistence and authorization
is represented only by references to the existing approval control plane.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .privacy import redact_sensitive_text

CHANNELS = {"cli", "http", "web", "mobile", "voice"}
_AUTH_REF_RE = re.compile(
    r"(?:auth|op|val|task|intent|evo|req)-[A-Za-z0-9._-]{3,127}"
)
_IDEMPOTENCY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{7,127}")
_REQUEST_RE = re.compile(r"cmd-[0-9a-f]{16}")
_SAFE_METADATA_KEYS = {"device_id", "locale", "transcript_confidence", "client_version"}


class ChannelEnvelopeError(ValueError):
    """Invalid or conflicting command-envelope request."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_text(value: object, limit: int = 4000) -> str:
    text = redact_sensitive_text(value).strip()
    if len(text) > limit:
        return text[: max(0, limit - 1)] + "…"
    return text


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _atomic_json(path: Path, value: dict[str, Any], *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if exclusive:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(path, flags, 0o600)
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


class CommandEnvelopeStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.requests_dir = self.root / "data" / "channel-requests"
        self.idempotency_dir = self.root / "data" / "channel-idempotency"
        self.audit_path = self.root / "logs" / "channel-envelope.log"
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.idempotency_dir.mkdir(parents=True, exist_ok=True)

    def _audit(self, event: str, request_id: str, detail: str = "") -> None:
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"[{_now_iso()}] event={event} request={request_id} "
                    f"{_safe_text(detail, 500)}\n"
                )
        except OSError:
            pass

    @staticmethod
    def _normalize_metadata(value: object) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        result: dict[str, Any] = {}
        for key in _SAFE_METADATA_KEYS:
            if key not in value:
                continue
            raw = value[key]
            if key == "transcript_confidence":
                try:
                    result[key] = max(0.0, min(float(raw), 1.0))
                except (TypeError, ValueError):
                    continue
            else:
                result[key] = _safe_text(raw, 200)
        return result

    @staticmethod
    def _normalize_auth_refs(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        result: list[str] = []
        for raw in value[:20]:
            reference = str(raw or "").strip()
            if not _AUTH_REF_RE.fullmatch(reference):
                raise ChannelEnvelopeError(
                    f"授权引用格式非法：{reference!r}；只能引用现有 auth/op/val/task/intent/evo/req ID"
                )
            if reference not in result:
                result.append(reference)
        return result

    def _index_path(self, actor_id: str, session_id: str, idempotency_key: str) -> Path:
        digest = hashlib.sha256(
            f"{actor_id}\0{session_id}\0{idempotency_key}".encode("utf-8")
        ).hexdigest()
        return self.idempotency_dir / f"{digest}.json"

    def create(
        self,
        *,
        channel: str,
        actor_id: str,
        session_id: str,
        message: str,
        idempotency_key: str,
        authorization_refs: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        channel = str(channel or "").strip().lower()
        if channel not in CHANNELS:
            raise ChannelEnvelopeError(f"未知交互渠道：{channel}")
        actor_id = _safe_text(actor_id, 200)
        session_id = _safe_text(session_id, 200)
        safe_message = _safe_text(message, 8000)
        idempotency_key = str(idempotency_key or "").strip()
        if not actor_id or not session_id or not safe_message:
            raise ChannelEnvelopeError("actor_id、session_id 与 message 不能为空")
        if not _IDEMPOTENCY_RE.fullmatch(idempotency_key):
            raise ChannelEnvelopeError("idempotency_key 必须为 8-128 位安全字符")
        auth_refs = self._normalize_auth_refs(authorization_refs or [])
        safe_metadata = self._normalize_metadata(metadata or {})
        canonical = {
            "channel": channel,
            "actor_id": actor_id,
            "session_id": session_id,
            "message": safe_message,
            "idempotency_key": idempotency_key,
            "authorization_refs": auth_refs,
            "metadata": safe_metadata,
        }
        payload_hash = _canonical_hash(canonical)
        index_path = self._index_path(actor_id, session_id, idempotency_key)
        if index_path.is_file():
            try:
                index = json.loads(index_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ChannelEnvelopeError("幂等索引损坏，拒绝重复执行") from exc
            if index.get("payload_hash") != payload_hash:
                self._audit(
                    "idempotency_conflict",
                    str(index.get("request_id", "unknown")),
                    f"actor={actor_id} session={session_id}",
                )
                raise ChannelEnvelopeError(
                    "同一 actor/session 的 idempotency_key 已用于不同载荷"
                )
            existing = self.get(str(index.get("request_id", "")))
            return {**existing, "replayed": True}
        request_id = "cmd-" + uuid.uuid4().hex[:16]
        envelope = {
            "schema_version": 1,
            "id": request_id,
            **canonical,
            "payload_hash": payload_hash,
            "created_at": _now_iso(),
            "status": "accepted",
            "replayed": False,
            "credentials_redacted": safe_message != str(message or "").strip(),
            "authorization_is_reference_only": True,
        }
        _atomic_json(self.requests_dir / f"{request_id}.json", envelope, exclusive=True)
        _atomic_json(
            index_path,
            {"request_id": request_id, "payload_hash": payload_hash, "created_at": envelope["created_at"]},
            exclusive=True,
        )
        self._audit("accepted", request_id, f"channel={channel} actor={actor_id}")
        return envelope

    def get(self, request_id: str) -> dict[str, Any]:
        request_id = str(request_id or "").strip()
        if not _REQUEST_RE.fullmatch(request_id):
            raise ChannelEnvelopeError(f"非法命令信封 ID：{request_id!r}")
        try:
            value = json.loads(
                (self.requests_dir / f"{request_id}.json").read_text(encoding="utf-8")
            )
        except FileNotFoundError as exc:
            raise ChannelEnvelopeError(f"命令信封不存在：{request_id}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ChannelEnvelopeError(f"命令信封损坏：{request_id}") from exc
        if not isinstance(value, dict) or value.get("id") != request_id:
            raise ChannelEnvelopeError(f"命令信封结构非法：{request_id}")
        return value
