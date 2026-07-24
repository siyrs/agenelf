"""Privacy helpers for local personalization and long-term memory.

The Agent may receive owner-authored profile data, notes and chat messages, but
secrets must never be persisted into memory or injected into the model prompt.
This module deliberately uses conservative, dependency-free redaction rules.
"""

from __future__ import annotations

import re
from typing import Any

_SENSITIVE_KEY = re.compile(
    r"(?:password|passwd|passphrase|secret|token|api[_-]?key|private[_-]?key|credential|cookie)",
    re.IGNORECASE,
)

_TEXT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"), "sk-[REDACTED]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{8,}\b"), "gh_[REDACTED]"),
    (re.compile(r"\bAKIA[0-9A-Z]{12,}\b"), "AKIA[REDACTED]"),
    (
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
        "Bearer [REDACTED]",
    ),
    (
        re.compile(
            r"(?i)\b(password|passwd|passphrase|secret|token|api[_-]?key|private[_-]?key)\b\s*[:=]\s*([^\s,;]+)"
        ),
        r"\1=[REDACTED]",
    ),
)


def is_sensitive_key(key: object) -> bool:
    """Return whether a mapping key is likely to contain credentials."""

    return bool(_SENSITIVE_KEY.search(str(key or "")))


def redact_sensitive_text(value: object) -> str:
    """Redact common credential forms from arbitrary text."""

    text = str(value or "")
    for pattern, replacement in _TEXT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def sanitize_value(
    value: Any,
    *,
    path: str = "root",
    warnings: list[str] | None = None,
    max_depth: int = 8,
) -> Any:
    """Recursively sanitize a JSON/YAML-like value.

    Sensitive-key values are replaced rather than returned. Text values also go
    through pattern redaction. Unsupported objects are converted to safe text.
    """

    warnings = warnings if warnings is not None else []
    if max_depth < 0:
        warnings.append(f"{path}: 嵌套层级过深，已截断")
        return "[TRUNCATED]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, child in value.items():
            key = str(raw_key)
            child_path = f"{path}.{key}"
            if is_sensitive_key(key):
                result[key] = "[REDACTED]"
                warnings.append(f"{child_path}: 敏感字段已脱敏，不会进入模型上下文")
            else:
                result[key] = sanitize_value(
                    child,
                    path=child_path,
                    warnings=warnings,
                    max_depth=max_depth - 1,
                )
        return result
    if isinstance(value, list):
        return [
            sanitize_value(
                child,
                path=f"{path}[{index}]",
                warnings=warnings,
                max_depth=max_depth - 1,
            )
            for index, child in enumerate(value)
        ]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_sensitive_text(value)
