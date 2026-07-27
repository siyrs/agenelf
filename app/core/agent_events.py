"""Versioned Agent lifecycle events with bounded replay and Ledger persistence.

This module is the language-neutral event-core foundation for Agenelf's future
Node.js runtime and real SSE transport.  It intentionally does not patch
``Agent.chat`` or expose a model-callable tool; wiring belongs to a separate,
small follow-up change.

Design goals inspired by Pi's event-first runtime:

- one stable envelope for Web, CLI, audit and future RPC consumers;
- monotonically increasing per-run sequence numbers;
- bounded in-memory replay with explicit cursor-expiry errors;
- exactly one terminal event per run;
- transient streaming deltas are not persisted by default;
- durable lifecycle events are appended to the owner-local Session Ledger;
- payloads are recursively redacted and size-bounded before publication.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .privacy import sanitize_value
from .session_ledger import SessionLedgerStore
from .storage import now_iso

SCHEMA_VERSION = 1
DEFAULT_MAX_BUFFER_EVENTS = 2_000
MAX_BUFFER_EVENTS = 20_000
DEFAULT_READ_LIMIT = 200
MAX_READ_LIMIT = 1_000
MAX_PAYLOAD_BYTES = 64 * 1024
DEFAULT_MAX_RUNS = 128
MAX_RUNS = 2_000

EVENT_TYPES = {
    "run.started",
    "turn.started",
    "reasoning.started",
    "reasoning.delta",
    "reasoning.completed",
    "message.delta",
    "message.completed",
    "tool.preflight",
    "tool.started",
    "tool.delta",
    "tool.completed",
    "approval.required",
    "approval.resolved",
    "runner.started",
    "runner.completed",
    "run.checkpointed",
    "run.compacted",
    "run.settled",
    "run.failed",
    "run.cancelled",
}
TERMINAL_EVENT_TYPES = {"run.settled", "run.failed", "run.cancelled"}
TRANSIENT_EVENT_TYPES = {"reasoning.delta", "message.delta", "tool.delta"}
ORIGINS = {"runtime", "agent_skill", "owner", "runner", "migration"}

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_RUN_ID_RE = re.compile(r"^run-[0-9a-f]{16}$")
_EVENT_ID_RE = re.compile(r"^aevt-[0-9a-f]{20}$")


class AgentEventError(ValueError):
    """Base error for invalid event-core operations."""


class EventCursorExpired(AgentEventError):
    """Raised when a replay cursor predates the bounded in-memory buffer."""


class RunAlreadyTerminal(AgentEventError):
    """Raised when an event is emitted after the run has reached a terminal state."""


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _safe_session_id(value: object) -> str:
    session_id = str(value or "").strip()
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise AgentEventError(
            "session_id 只能包含字母、数字、点、下划线、连字符，"
            "以字母或数字开头，长度 1-64"
        )
    return session_id


def _safe_run_id(value: object | None = None) -> str:
    run_id = str(value or "").strip() or ("run-" + uuid.uuid4().hex[:16])
    if not _RUN_ID_RE.fullmatch(run_id):
        raise AgentEventError(f"非法 run_id：{run_id!r}")
    return run_id


def _safe_event_type(value: object) -> str:
    event_type = str(value or "").strip()
    if event_type not in EVENT_TYPES:
        raise AgentEventError(f"未知 Agent event type：{event_type!r}")
    return event_type


def _safe_origin(value: object) -> str:
    origin = str(value or "").strip()
    if origin not in ORIGINS:
        raise AgentEventError(f"未知 Agent event origin：{origin!r}")
    return origin


def _safe_payload(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AgentEventError("Agent event payload 必须是 JSON object")
    warnings: list[str] = []
    safe = sanitize_value(value, path="payload", warnings=warnings, max_depth=8)
    if not isinstance(safe, dict):
        raise AgentEventError("Agent event payload 清洗后不是 object")
    if warnings:
        safe = {**safe, "_privacy_warnings": warnings[:20]}
    encoded = json.dumps(
        safe,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise AgentEventError(
            f"Agent event payload 超过 {MAX_PAYLOAD_BYTES} 字节上限"
        )
    return safe


@dataclass(frozen=True)
class AgentEvent:
    """Stable event envelope shared by Python, future TypeScript and transports."""

    schema_version: int
    id: str
    session_id: str
    run_id: str
    seq: int
    type: str
    origin: str
    ts: str
    transient: bool
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RunEventStream:
    """Thread-safe bounded event stream for exactly one Agent run."""

    def __init__(
        self,
        *,
        root: str | Path,
        session_id: str,
        run_id: str | None = None,
        max_buffer_events: int = DEFAULT_MAX_BUFFER_EVENTS,
        persist_durable_events: bool = True,
    ) -> None:
        self.root = Path(root).resolve()
        self.session_id = _safe_session_id(session_id)
        self.run_id = _safe_run_id(run_id)
        self.max_buffer_events = _bounded_int(
            max_buffer_events,
            DEFAULT_MAX_BUFFER_EVENTS,
            1,
            MAX_BUFFER_EVENTS,
        )
        self.persist_durable_events = bool(persist_durable_events)
        self._ledger = SessionLedgerStore(self.root)
        self._events: deque[AgentEvent] = deque(maxlen=self.max_buffer_events)
        self._condition = threading.Condition(threading.RLock())
        self._next_seq = 1
        self._terminal_type: str | None = None

    @property
    def terminal_type(self) -> str | None:
        with self._condition:
            return self._terminal_type

    @property
    def last_seq(self) -> int:
        with self._condition:
            return self._next_seq - 1

    @property
    def is_terminal(self) -> bool:
        return self.terminal_type is not None

    def _persist(self, event: AgentEvent) -> None:
        if not self.persist_durable_events or event.transient:
            return
        self._ledger.append(
            self.session_id,
            "custom",
            {"agent_event": event.to_dict()},
            origin=event.origin,
        )

    def emit(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        origin: str = "runtime",
        transient: bool | None = None,
    ) -> AgentEvent:
        """Create, optionally persist, publish and wake replay subscribers.

        Durable persistence happens before the event becomes visible in memory.  If
        persistence fails, sequence state is unchanged and consumers never observe a
        non-durable event pretending to have been committed.
        """

        event_type = _safe_event_type(event_type)
        origin = _safe_origin(origin)
        safe_payload = _safe_payload(payload or {})
        is_transient = (
            event_type in TRANSIENT_EVENT_TYPES
            if transient is None
            else bool(transient)
        )

        with self._condition:
            if self._terminal_type is not None:
                raise RunAlreadyTerminal(
                    f"run {self.run_id} 已以 {self._terminal_type} 结束"
                )
            event = AgentEvent(
                schema_version=SCHEMA_VERSION,
                id="aevt-" + uuid.uuid4().hex[:20],
                session_id=self.session_id,
                run_id=self.run_id,
                seq=self._next_seq,
                type=event_type,
                origin=origin,
                ts=now_iso(),
                transient=is_transient,
                payload=safe_payload,
            )
            self._persist(event)
            self._events.append(event)
            self._next_seq += 1
            if event_type in TERMINAL_EVENT_TYPES:
                self._terminal_type = event_type
            self._condition.notify_all()
            return event

    def _check_cursor_locked(self, after_seq: int) -> None:
        if after_seq < 0:
            raise AgentEventError("after_seq 不能为负数")
        if not self._events:
            return
        oldest = self._events[0].seq
        if after_seq < oldest - 1:
            raise EventCursorExpired(
                f"事件游标 {after_seq} 已早于内存缓冲区起点 {oldest - 1}；"
                "请从 Session Ledger 或持久化事件存储恢复"
            )

    def _events_after_locked(self, after_seq: int, limit: int) -> list[AgentEvent]:
        self._check_cursor_locked(after_seq)
        return [event for event in self._events if event.seq > after_seq][:limit]

    def events_after(
        self,
        after_seq: int = 0,
        *,
        limit: int = DEFAULT_READ_LIMIT,
    ) -> list[dict[str, Any]]:
        bounded_limit = _bounded_int(
            limit, DEFAULT_READ_LIMIT, 1, MAX_READ_LIMIT
        )
        with self._condition:
            return [
                event.to_dict()
                for event in self._events_after_locked(int(after_seq), bounded_limit)
            ]

    def wait_after(
        self,
        after_seq: int = 0,
        *,
        timeout_seconds: float | None = None,
        limit: int = DEFAULT_READ_LIMIT,
    ) -> list[dict[str, Any]]:
        """Wait for events after a cursor, a terminal state, or timeout.

        ``timeout_seconds=None`` waits indefinitely.  A terminal run with no newer
        events returns immediately with an empty list, allowing SSE generators to
        close without polling.
        """

        bounded_limit = _bounded_int(
            limit, DEFAULT_READ_LIMIT, 1, MAX_READ_LIMIT
        )
        cursor = int(after_seq)
        deadline = (
            None
            if timeout_seconds is None
            else time.monotonic() + max(0.0, float(timeout_seconds))
        )
        with self._condition:
            while True:
                values = self._events_after_locked(cursor, bounded_limit)
                if values:
                    return [event.to_dict() for event in values]
                if self._terminal_type is not None:
                    return []
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._condition.wait(remaining)

    def snapshot(self) -> dict[str, Any]:
        with self._condition:
            oldest_seq = self._events[0].seq if self._events else None
            return {
                "schema_version": SCHEMA_VERSION,
                "session_id": self.session_id,
                "run_id": self.run_id,
                "last_seq": self._next_seq - 1,
                "oldest_buffered_seq": oldest_seq,
                "buffered_events": len(self._events),
                "max_buffer_events": self.max_buffer_events,
                "terminal_type": self._terminal_type,
                "persist_durable_events": self.persist_durable_events,
            }


class AgentEventHub:
    """Bounded registry of run streams; evicts only terminal runs."""

    def __init__(
        self,
        *,
        root: str | Path,
        max_runs: int = DEFAULT_MAX_RUNS,
        max_buffer_events: int = DEFAULT_MAX_BUFFER_EVENTS,
    ) -> None:
        self.root = Path(root).resolve()
        self.max_runs = _bounded_int(max_runs, DEFAULT_MAX_RUNS, 1, MAX_RUNS)
        self.max_buffer_events = _bounded_int(
            max_buffer_events,
            DEFAULT_MAX_BUFFER_EVENTS,
            1,
            MAX_BUFFER_EVENTS,
        )
        self._runs: OrderedDict[str, RunEventStream] = OrderedDict()
        self._lock = threading.RLock()

    def _evict_terminal_locked(self) -> None:
        while len(self._runs) >= self.max_runs:
            removable = next(
                (
                    run_id
                    for run_id, stream in self._runs.items()
                    if stream.is_terminal
                ),
                None,
            )
            if removable is None:
                raise AgentEventError(
                    "活动 run 数量已达到上限，且没有可安全驱逐的终态 run"
                )
            self._runs.pop(removable, None)

    def create(
        self,
        session_id: str,
        *,
        run_id: str | None = None,
        persist_durable_events: bool = True,
    ) -> RunEventStream:
        stream = RunEventStream(
            root=self.root,
            session_id=session_id,
            run_id=run_id,
            max_buffer_events=self.max_buffer_events,
            persist_durable_events=persist_durable_events,
        )
        with self._lock:
            if stream.run_id in self._runs:
                raise AgentEventError(f"run 已存在：{stream.run_id}")
            self._evict_terminal_locked()
            self._runs[stream.run_id] = stream
            return stream

    def get(self, run_id: str) -> RunEventStream:
        run_id = _safe_run_id(run_id)
        with self._lock:
            stream = self._runs.get(run_id)
            if stream is None:
                raise AgentEventError(f"run 不存在或已从内存驱逐：{run_id}")
            self._runs.move_to_end(run_id)
            return stream

    def list_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [stream.snapshot() for stream in self._runs.values()]

    def remove(self, run_id: str) -> bool:
        run_id = _safe_run_id(run_id)
        with self._lock:
            stream = self._runs.get(run_id)
            if stream is None:
                return False
            if not stream.is_terminal:
                raise AgentEventError("不能从 Hub 删除仍在活动的 run")
            self._runs.pop(run_id, None)
            return True


def validate_event_envelope(value: object) -> dict[str, Any]:
    """Defensive runtime validation for imported/replayed event dictionaries."""

    if not isinstance(value, dict):
        raise AgentEventError("Agent event envelope 必须是 object")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise AgentEventError("不支持的 Agent event schema_version")
    event_id = str(value.get("id", ""))
    if not _EVENT_ID_RE.fullmatch(event_id):
        raise AgentEventError("非法 Agent event id")
    _safe_session_id(value.get("session_id"))
    _safe_run_id(value.get("run_id"))
    try:
        seq = int(value.get("seq"))
    except (TypeError, ValueError) as exc:
        raise AgentEventError("Agent event seq 必须是正整数") from exc
    if seq < 1:
        raise AgentEventError("Agent event seq 必须是正整数")
    _safe_event_type(value.get("type"))
    _safe_origin(value.get("origin"))
    if not isinstance(value.get("transient"), bool):
        raise AgentEventError("Agent event transient 必须是 boolean")
    payload = _safe_payload(value.get("payload"))
    ts = str(value.get("ts", "")).strip()
    if not ts:
        raise AgentEventError("Agent event ts 不能为空")
    return {**value, "seq": seq, "payload": payload}
