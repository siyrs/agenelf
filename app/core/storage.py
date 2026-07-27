"""Shared JSON persistence and text-hygiene helpers for Agenelf.

These functions centralize boilerplate that used to be duplicated across
``core/`` and ``skills/`` modules (``_now_iso`` / ``_read_json`` /
``_atomic_json`` / ``_safe_text``).  Semantics deliberately match the most
common historical variants so existing callers keep their behavior:

- :func:`now_iso` returns a UTC ISO-8601 timestamp with second precision
  (``datetime.now(timezone.utc).isoformat(timespec="seconds")``).  Note that
  a few modules (``code_repair``, ``validation``) intentionally keep their
  own *local-timezone* ``now_iso`` for human-facing audit logs; this helper
  is the UTC variant used for machine-readable records.
- :func:`read_json` parses a JSON file and returns ``default`` when the file
  is missing, unreadable or contains invalid JSON.  The parsed value is
  returned as-is with **no type filtering** — callers that historically
  required a ``dict`` must keep an ``isinstance`` check.
- :func:`atomic_write_json` serializes with ``ensure_ascii=False,
  indent=2`` plus a trailing newline and writes via a hidden ``mkstemp``
  temporary file in the target directory followed by ``os.replace``, so a
  crash never leaves a half-written file at ``path``.  The temporary file is
  removed again if writing fails.  With ``exclusive=True`` the file must not
  exist yet: it is created directly with ``O_CREAT | O_EXCL`` (mode
  ``0o600``) and ``FileExistsError`` propagates to the caller, matching the
  previous queue-style create-once semantics.  Parent directories are
  created as needed.
- :func:`safe_text` coerces any value to ``str``, strips surrounding
  whitespace and truncates over-long text with a single trailing ellipsis
  (``…``).  Modules that need pre-processing (sensitive-text redaction or
  whitespace collapsing) apply it *before* calling this helper.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = ["now_iso", "read_json", "atomic_write_json", "safe_text"]


def now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string, second precision."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path, default: Any = None) -> Any:
    """Parse ``path`` as JSON, returning ``default`` on any failure.

    ``default`` is returned when the file does not exist, cannot be read or
    does not contain valid JSON.  A successfully parsed value is returned
    unchanged (no type filtering); use ``isinstance`` at the call site when a
    specific shape is required.
    """

    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def atomic_write_json(path: Path, data: Any, *, exclusive: bool = False) -> None:
    """Write ``data`` as pretty-printed UTF-8 JSON to ``path`` atomically.

    The JSON is serialized with ``ensure_ascii=False, indent=2`` and a
    trailing newline.  Normally the payload goes to a hidden temporary file
    in the target directory and is moved into place with ``os.replace``
    (atomic on POSIX); the temporary file is cleaned up on failure so no
    ``*.tmp`` residue remains.  Parent directories are created as needed.

    With ``exclusive=True`` the target is instead created directly with
    ``O_WRONLY | O_CREAT | O_EXCL`` (mode ``0o600``): if ``path`` already
    exists, ``FileExistsError`` is raised and the existing file is left
    untouched.  This create-once mode is used for queue request/result files
    that must never be overwritten.
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if exclusive:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        return
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=".tmp", dir=path.parent, text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def safe_text(value: object, limit: int = 2000) -> str:
    """Coerce ``value`` to text, strip it and cap it at ``limit`` characters.

    ``None``/falsy values become ``""``.  Text longer than ``limit`` is cut
    to ``limit - 1`` characters and terminated with a single ``…``.  No other
    normalization is applied: callers needing credential redaction or
    whitespace collapsing must pre-process the value themselves.
    """

    text = str(value or "").strip()
    if len(text) > limit:
        return text[: max(0, limit - 1)] + "…"
    return text
