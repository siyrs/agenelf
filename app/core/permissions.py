"""Human approval primitives and conservative local-command classification.

Approval is deliberately split across three directories:

* ``auth-requests`` is writable by the Agent and contains proposals.
* ``auth-decisions`` is written by the host-side ``approve.sh`` and mounted
  read-only into the Agent container.
* ``auth-consumed`` stores one-time-use markers.

Every approval is bound to a canonical payload fingerprint.  An approval for
one command, target, or parameter set can never authorize another operation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_TTL_SECONDS = 300
MAX_PENDING_REQUESTS = 10

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_DENIED = "denied"
STATUS_EXPIRED = "expired"
STATUS_USED = "used"
STATUS_NOT_FOUND = "not_found"
STATUS_BINDING_MISMATCH = "binding_mismatch"

_WHITELIST_PROGS = {
    "ls",
    "ps",
    "df",
    "free",
    "uptime",
    "cat",
    "grep",
    "tail",
    "ss",
    "uname",
    "whoami",
    "pwd",
    "echo",
    "date",
    "hostname",
    "id",
    "w",
    "last",
    "dig",
    "nslookup",
}
_SHELL_META_CHARS = (">", "<", "|", ";", "&", "`", "$(", "\n")
_SYSTEM_PATH_RE = re.compile(r"^/(?:etc|usr|bin|sbin|boot|root)(?:/|$)")
_DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:rm|rmdir)\b"), "rm/rmdir 删除操作"),
    (re.compile(r"\bdd\b"), "dd 低层写操作"),
    (re.compile(r"\bmkfs(?:\.\w+)?\b"), "mkfs 格式化磁盘"),
    (re.compile(r"\b(?:shutdown|reboot|halt|poweroff)\b"), "关机/重启"),
    (re.compile(r"\b(?:kill|killall|pkill)\b"), "杀进程"),
    (re.compile(r"\b(?:chmod|chown)\b"), "权限或属主变更"),
    (re.compile(r"\b(?:useradd|userdel|usermod|passwd|visudo)\b"), "账号管理"),
    (re.compile(r"\b(?:iptables|nft|ufw)\b"), "防火墙变更"),
    (
        re.compile(r"\bsystemctl\b[^|;&]*\b(?:stop|restart|disable|mask)\b"),
        "systemd 破坏性动作",
    ),
    (
        re.compile(r"(?:>{1,2}|\btee\b)\s*/(?:etc|usr|bin|sbin|boot|root)(?:/|\s|$)"),
        "写入系统路径",
    ),
    (
        re.compile(r"\b(?:curl|wget)\b[^|]*\|\s*(?:sudo\s+)?(?:ba)?sh\b"),
        "远程脚本管道执行",
    ),
    (
        re.compile(r"\bgit\b[^|;&]*\bpush\b[^|;&]*(?:--force\b|-f\b)"),
        "git 强制推送",
    ),
    (re.compile(r":\s*\(\s*\)\s*\{[^}]*:\s*\|\s*:"), "fork 炸弹"),
    (
        re.compile(r"\b(?:pip3?|npm|apt(?:-get)?|yum|dnf)\b[^|;&]*\binstall\b"),
        "安装软件包",
    ),
]


def _now() -> datetime:
    return datetime.now().astimezone()


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _root() -> Path:
    configured = os.environ.get("AGENELF_ROOT", "").strip()
    if configured:
        return Path(configured).resolve()
    return Path(__file__).resolve().parents[2]


def _directory(name: str, root: Path | None = None) -> Path:
    return (root or _root()) / "data" / name


def _path(directory: str, request_id: str, root: Path | None = None) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", str(request_id or "")):
        raise ValueError(f"非法授权请求 ID：{request_id!r}")
    return _directory(directory, root) / f"{request_id}.json"


def _read(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write(path: Path, data: dict[str, Any], exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if exclusive:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        return
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def canonical_binding(binding: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(binding, dict):
        raise TypeError("binding 必须是对象")
    return json.loads(json.dumps(binding, ensure_ascii=False, sort_keys=True))


def binding_fingerprint(binding: dict[str, Any]) -> str:
    encoded = json.dumps(
        canonical_binding(binding),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _mv_cp_targets_system(command: str) -> bool:
    for segment in re.split(r"[|;&]", command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            continue
        if len(tokens) >= 3 and tokens[0] in ("mv", "cp"):
            if _SYSTEM_PATH_RE.match(tokens[-1]):
                return True
    return False


def classify_command(command: str) -> str:
    """Classify local commands as whitelist, normal, or dangerous."""

    if not isinstance(command, str) or not command.strip():
        return "normal"
    text = command.strip()
    for pattern, _description in _DANGEROUS_PATTERNS:
        if pattern.search(text):
            return "dangerous"
    if _mv_cp_targets_system(text):
        return "dangerous"
    if any(token in text for token in _SHELL_META_CHARS):
        return "normal"
    try:
        argv = shlex.split(text)
    except ValueError:
        return "normal"
    if not argv:
        return "normal"
    program = argv[0]
    if program in _WHITELIST_PROGS:
        return "whitelist"
    if program == "curl" and ("-I" in argv[1:] or "--head" in argv[1:]):
        return "whitelist"
    if program == "systemctl" and len(argv) >= 2 and argv[1] == "status":
        return "whitelist"
    if program == "ping" and "-c" in argv[1:]:
        return "whitelist"
    return "normal"


def _count_pending(root: Path | None = None) -> int:
    requests = _directory("auth-requests", root)
    decisions = _directory("auth-decisions", root)
    if not requests.is_dir():
        return 0
    count = 0
    for path in requests.glob("*.json"):
        request_id = path.stem
        if not (decisions / f"{request_id}.json").exists():
            count += 1
    return count


def request_auth(
    skill: str,
    action: str,
    detail: str,
    reason: str = "",
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    binding: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Create a proposal; only host-side approval can create a decision."""

    root = _root()
    pending = _count_pending(root)
    if pending >= MAX_PENDING_REQUESTS:
        return False, f"待裁决授权请求已达 {pending} 条（上限 {MAX_PENDING_REQUESTS}）"
    request_id = f"auth-{uuid.uuid4().hex[:12]}"
    bound = canonical_binding(
        binding
        or {
            "skill": str(skill),
            "action": str(action),
            "detail": str(detail),
        }
    )
    now = _now()
    data = {
        "schema_version": 2,
        "id": request_id,
        "skill": str(skill),
        "action": str(action),
        "detail": str(detail),
        "reason": str(reason),
        "binding": bound,
        "fingerprint": binding_fingerprint(bound),
        "created_at": _iso(now),
        "expires_at": _iso(now + timedelta(seconds=max(1, int(ttl_seconds)))),
    }
    try:
        _write(_path("auth-requests", request_id, root), data, exclusive=True)
    except OSError as exc:
        return False, f"授权请求创建失败：{exc}"
    audit("auth_request", f"{request_id} skill={skill} action={action}")
    return True, request_id


def _expired(timestamp: Any) -> bool:
    try:
        expires_at = datetime.fromisoformat(str(timestamp))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc).astimezone()
        return _now() > expires_at
    except ValueError:
        return True


def check_auth(
    request_id: str,
    expected_binding: dict[str, Any] | None = None,
) -> str:
    root = _root()
    try:
        request = _read(_path("auth-requests", request_id, root))
    except ValueError:
        return STATUS_NOT_FOUND
    if request is None:
        return STATUS_NOT_FOUND
    if _path("auth-consumed", request_id, root).exists():
        return STATUS_USED

    decision = _read(_path("auth-decisions", request_id, root))
    if decision is None:
        return STATUS_EXPIRED if _expired(request.get("expires_at")) else STATUS_PENDING
    if decision.get("decision") == "deny":
        return STATUS_DENIED
    if decision.get("decision") != "approve":
        return STATUS_NOT_FOUND
    if _expired(decision.get("expires_at")):
        return STATUS_EXPIRED

    actual_fingerprint = str(decision.get("fingerprint", ""))
    request_fingerprint = binding_fingerprint(request.get("binding", {}))
    if actual_fingerprint != request_fingerprint:
        return STATUS_BINDING_MISMATCH
    if expected_binding is not None:
        if actual_fingerprint != binding_fingerprint(expected_binding):
            return STATUS_BINDING_MISMATCH
    return STATUS_APPROVED


def consume_auth(
    request_id: str,
    expected_binding: dict[str, Any] | None = None,
) -> bool:
    """Atomically consume an approved, exact-match authorization once."""

    if check_auth(request_id, expected_binding=expected_binding) != STATUS_APPROVED:
        return False
    request = _read(_path("auth-requests", request_id)) or {}
    marker = {
        "id": request_id,
        "consumed_at": _iso(_now()),
        "fingerprint": binding_fingerprint(
            expected_binding or request.get("binding", {})
        ),
    }
    try:
        _write(_path("auth-consumed", request_id), marker, exclusive=True)
    except (FileExistsError, OSError, AttributeError):
        return False
    audit("auth_consumed", f"{request_id} 一次性授权已核销")
    return True


def audit(event: str, detail: str) -> None:
    path = _root() / "logs" / "audit.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{_iso(_now())}] [{event}] {detail}\n")
    except OSError:
        pass
