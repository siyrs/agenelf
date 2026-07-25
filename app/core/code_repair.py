"""File-backed queue and configuration helpers for isolated code repair.

The Agent may submit only a unified diff against an owner-configured repository
alias.  A separate deterministic runner copies the read-only source repository,
applies the exact patch, runs owner-configured test commands without network or
credentials, and writes trusted evidence.  This module never mutates source
repositories, commits, pushes, or merges code.
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

import yaml

from .privacy import redact_sensitive_text

_CAPABILITY = "code.repair"
_OPERATION = "apply_patch_and_test"
_ID_RE = re.compile(r"repair-[0-9a-f]{16}")
_ALIAS_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_BASE_RE = re.compile(r"[0-9a-fA-F]{7,64}")
_MAX_PATCH_BYTES = 262_144


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def runtime_root() -> Path:
    configured = os.environ.get("AGENELF_ROOT", "").strip()
    return Path(configured).resolve() if configured else Path(__file__).resolve().parents[2]


def repository_config_path(root: Path | None = None) -> Path:
    configured = os.environ.get("AGENELF_REPOSITORIES_FILE", "").strip()
    if configured:
        return Path(configured).resolve()
    return (root or runtime_root()).resolve() / "local" / "repositories.yaml"


def queue_paths(root: Path | None = None) -> dict[str, Path]:
    base = (root or runtime_root()).resolve()
    data = base / "data"
    return {
        "requests": data / "repair-requests",
        "results": data / "repair-results",
        "locks": data / "repair-locks",
        "audit": base / "logs" / "repair.log",
    }


def load_repair_config(path: str | Path | None = None) -> dict[str, Any]:
    config_path = Path(path).resolve() if path else repository_config_path()
    if not config_path.is_file() or config_path.is_symlink():
        return {"schema_version": 1, "repositories": {}, "test_profiles": {}}
    try:
        value = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"无法读取代码仓库配置：{exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("代码仓库配置顶层必须是对象")
    repositories = value.get("repositories", {})
    test_profiles = value.get("test_profiles", {})
    if not isinstance(repositories, dict) or not isinstance(test_profiles, dict):
        raise ValueError("repositories 与 test_profiles 必须是对象")
    return {
        "schema_version": int(value.get("schema_version", 1)),
        "repositories": repositories,
        "test_profiles": test_profiles,
    }


def safe_catalog(config: dict[str, Any]) -> dict[str, Any]:
    values: list[dict[str, Any]] = []
    repositories = config.get("repositories", {})
    if not isinstance(repositories, dict):
        repositories = {}
    for raw_alias, raw_profile in sorted(repositories.items()):
        alias = str(raw_alias)
        if not _ALIAS_RE.fullmatch(alias) or not isinstance(raw_profile, dict):
            continue
        allowed = raw_profile.get("allowed_test_profiles", [])
        values.append(
            {
                "alias": alias,
                "description": redact_sensitive_text(raw_profile.get("description", ""))[:500],
                "default_test_profile": str(raw_profile.get("default_test_profile", "")),
                "allowed_test_profiles": [str(item) for item in allowed[:20]]
                if isinstance(allowed, list)
                else [],
                "language": str(raw_profile.get("language", ""))[:100],
            }
        )
    return {"schema_version": 1, "repositories": values, "credentials_exposed": False}


def patch_sha256(unified_diff: str) -> str:
    return hashlib.sha256(unified_diff.encode("utf-8")).hexdigest()


def canonical_payload(
    repository: str,
    test_profile: str,
    patch_digest: str,
    patch_bytes: int,
    expected_base: str = "",
) -> dict[str, Any]:
    repository = str(repository or "").strip()
    test_profile = str(test_profile or "").strip()
    expected_base = str(expected_base or "").strip().lower()
    if not _ALIAS_RE.fullmatch(repository):
        raise ValueError(f"非法仓库别名：{repository!r}")
    if not _ALIAS_RE.fullmatch(test_profile):
        raise ValueError(f"非法测试配置别名：{test_profile!r}")
    if not re.fullmatch(r"[0-9a-f]{64}", str(patch_digest or "")):
        raise ValueError("补丁摘要格式非法")
    if not 1 <= int(patch_bytes) <= _MAX_PATCH_BYTES:
        raise ValueError(f"补丁大小必须在 1..{_MAX_PATCH_BYTES} 字节")
    if expected_base and not _BASE_RE.fullmatch(expected_base):
        raise ValueError("expected_base 必须是 7-64 位 Git commit SHA")
    return {
        "capability": _CAPABILITY,
        "operation": _OPERATION,
        "target": repository,
        "parameters": {
            "test_profile": test_profile,
            "patch_sha256": patch_digest,
            "patch_bytes": int(patch_bytes),
            "expected_base": expected_base,
        },
    }


def payload_fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_id(repair_id: str) -> str:
    value = str(repair_id or "").strip()
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"非法代码修复 ID：{repair_id!r}")
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
            handle.write(f"[{now_iso()}] [{event}] {redact_sensitive_text(detail)}\n")
    except OSError:
        pass


def submit_repair(
    repository: str,
    unified_diff: str,
    test_profile: str,
    summary: str,
    *,
    expected_base: str = "",
    root: Path | None = None,
) -> dict[str, Any]:
    patch = str(unified_diff or "")
    patch_bytes = len(patch.encode("utf-8"))
    if "\x00" in patch:
        raise ValueError("补丁不得包含 NUL 字节")
    if "diff --git " not in patch:
        raise ValueError("必须提交 git unified diff（缺少 diff --git）")
    if patch_bytes > _MAX_PATCH_BYTES:
        raise ValueError(f"补丁超过全局上限 {_MAX_PATCH_BYTES} 字节")
    if redact_sensitive_text(patch) != patch:
        raise ValueError("补丁包含疑似凭据，拒绝进入代码修复队列")
    digest = patch_sha256(patch)
    payload = canonical_payload(
        repository,
        test_profile,
        digest,
        patch_bytes,
        expected_base=expected_base,
    )
    repair_id = f"repair-{uuid.uuid4().hex[:16]}"
    request = {
        "schema_version": 1,
        "id": repair_id,
        **payload,
        "risk": "read",
        "summary": redact_sensitive_text(summary)[:1000],
        "patch": patch,
        "fingerprint": payload_fingerprint(payload),
        "created_at": now_iso(),
        "created_by": "agenelf-agent",
    }
    path = queue_paths(root)["requests"] / f"{repair_id}.json"
    _atomic_json(path, request, exclusive=True)
    audit(
        "repair_submitted",
        f"{repair_id} repository={repository} profile={test_profile} patch={digest}",
        root,
    )
    return request


def get_repair(repair_id: str, *, root: Path | None = None) -> dict[str, Any]:
    repair_id = _validate_id(repair_id)
    paths = queue_paths(root)
    request = read_json(paths["requests"] / f"{repair_id}.json")
    if request is None:
        return {"id": repair_id, "status": "not_found"}
    result = read_json(paths["results"] / f"{repair_id}.json")
    if result is not None:
        return {
            "id": repair_id,
            "status": str(result.get("status", "finished")),
            "request": {key: value for key, value in request.items() if key != "patch"},
            "result": result,
        }
    return {
        "id": repair_id,
        "status": "queued",
        "request": {key: value for key, value in request.items() if key != "patch"},
    }


def wait_for_repair(
    repair_id: str,
    timeout_seconds: float = 0,
    poll_interval: float = 0.2,
    *,
    root: Path | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while True:
        state = get_repair(repair_id, root=root)
        if state.get("result") is not None or state.get("status") in {
            "failed",
            "blocked",
            "not_found",
        }:
            return state
        if time.monotonic() >= deadline:
            return state
        time.sleep(max(0.05, float(poll_interval)))
