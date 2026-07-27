"""Exact owner authorization and server/Docker scope binding for continuation."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

from . import continuation_store as store

AUTH_TTL_MINUTES = 30
DOCKER_SKILL_AUTO_PROMOTE_PATHS = frozenset(
    {
        "skills/docker_ops.py",
        "tests/test_docker_ops.py",
        "tests/test_ops_runner_v2.py",
    }
)
_AUTH_ZH = re.compile(r"我(?:明确)?(?:授权|允许)你", re.IGNORECASE)
_AUTH_EN = re.compile(
    r"\b(?:i\s+authorize\s+you|you\s+are\s+authorized)\b", re.IGNORECASE
)
_DOCKER_WORD = re.compile(r"docker", re.IGNORECASE)
_SKILL_WORD = re.compile(r"(?:技能|skill)", re.IGNORECASE)
_CHANGE_WORD = re.compile(
    r"(?:升级|完善|修改|迭代|更新|upgrade|improve|modify|update)", re.IGNORECASE
)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_IDENTITY = re.compile(r"sha256:[0-9a-f]{64}")


def owner_authorizes_docker_skill(text: str) -> bool:
    value = str(text or "")
    explicit = bool(_AUTH_ZH.search(value) or _AUTH_EN.search(value))
    return bool(
        explicit
        and _DOCKER_WORD.search(value)
        and _SKILL_WORD.search(value)
        and _CHANGE_WORD.search(value)
    )


def authorization_record(text: str) -> dict[str, Any]:
    granted = owner_authorizes_docker_skill(text)
    return {
        "kind": "owner_scoped_docker_skill_upgrade",
        "granted": granted,
        "statement_sha256": store.sha256_text(text) if granted else "",
        "capabilities": ["agent.evolution", "server.docker"] if granted else [],
        "allowed_paths": (
            sorted(DOCKER_SKILL_AUTO_PROMOTE_PATHS) if granted else []
        ),
        "does_not_bypass_external_approval": True,
        "granted_at": store.now_iso() if granted else None,
    }


def _last_scope_path(root: Path) -> Path:
    return store.store_dir(root) / "last-scope.json"


def load_last_scope(root: Path) -> dict[str, Any]:
    value = store.read_json(_last_scope_path(root))
    return value if isinstance(value, dict) else {}


def save_last_scope(root: Path, scope: dict[str, Any]) -> None:
    safe = {
        key: scope.get(key)
        for key in (
            "capability",
            "target",
            "container",
            "service",
            "project",
            "profile_ref_fingerprint",
            "ssh_identity_fingerprint",
        )
        if scope.get(key)
    }
    if safe.get("target"):
        safe["updated_at"] = store.now_iso()
        store.atomic_json(_last_scope_path(root), safe)


def profile_ref_fingerprint(target: str) -> str:
    servers_file = os.environ.get("AGENELF_SERVERS_FILE", "").strip()
    path = Path(servers_file) if servers_file else store.runtime_root() / "local" / "servers.yaml"
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return ""
    profiles = document.get("servers", {}) if isinstance(document, dict) else {}
    profile = profiles.get(target) if isinstance(profiles, dict) else None
    if not isinstance(profile, dict):
        return ""
    auth = profile.get("auth", {}) if isinstance(profile.get("auth", {}), dict) else {}
    # Hash only public connection metadata and credential references.  The Agent
    # never reads password values, passphrases or private-key bytes.
    identity = {
        "host": str(profile.get("host", "")),
        "port": int(profile.get("port", 22) or 22),
        "username": str(profile.get("username", "")),
        "auth_type": str(auth.get("type", "private_key")),
        "private_key_ref": str(auth.get("private_key", "")),
        "password_env_ref": str(auth.get("password_env", "")),
        "known_hosts_ref": str(profile.get("known_hosts", "")),
    }
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def scope_candidate(
    registry: Any, tool_name: str, args: dict[str, Any]
) -> dict[str, Any]:
    contract = getattr(registry, "contract_for", lambda *_: None)(tool_name, args)
    capability = str(getattr(contract, "capability", "") or "")
    target = str(args.get("target", "") or "").strip()
    if not target or not capability.startswith("server."):
        return {}
    value: dict[str, Any] = {
        "capability": capability,
        "target": target,
        "profile_ref_fingerprint": profile_ref_fingerprint(target),
    }
    for key in ("container", "service", "project"):
        text = str(args.get(key, "") or "").strip()
        if text:
            value[key] = text
    return value


def scope_conflict(existing: dict[str, Any], candidate: dict[str, Any]) -> str:
    for key in (
        "target",
        "container",
        "service",
        "project",
        "profile_ref_fingerprint",
    ):
        old = str(existing.get(key, "") or "")
        new = str(candidate.get(key, "") or "")
        if old and new and old != new:
            return f"{key} 从 {old!r} 变为 {new!r}"
    return ""


def merge_scope(existing: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    result = dict(existing)
    for key, value in candidate.items():
        if value:
            result[key] = value
    result["updated_at"] = store.now_iso()
    return result


def identity_from_result(result: str) -> str:
    try:
        value = json.loads(result)
    except (TypeError, json.JSONDecodeError):
        return ""
    if not isinstance(value, dict):
        return ""
    nested = value.get("result") if isinstance(value.get("result"), dict) else value
    fingerprint = str(nested.get("ssh_identity_fingerprint", "") or "")
    return fingerprint if _IDENTITY.fullmatch(fingerprint) else ""


def _tree_files(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not root.is_dir():
        return result
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or ".pytest_cache" in relative.parts:
            continue
        if path.suffix in {".pyc", ".pyo"}:
            continue
        try:
            result[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return result


def changed_files(root: Path) -> list[str]:
    baseline = _tree_files(root / "app-fork")
    candidate = _tree_files(root / "app-tmp")
    return sorted(
        path
        for path in set(baseline) | set(candidate)
        if baseline.get(path) != candidate.get(path)
    )


def write_promotion_authorization(
    root: Path, checkpoint: dict[str, Any], tool_result: str
) -> None:
    if "晋升请求已提交" not in tool_result:
        return
    authorization = checkpoint.get("authorization", {})
    if not isinstance(authorization, dict) or not authorization.get("granted"):
        store.add_event(
            checkpoint,
            "promotion_not_authorized",
            "本轮没有明确的 Docker 技能升级授权",
        )
        return
    current_scope = checkpoint.get("scope", {})
    if (
        not isinstance(current_scope, dict)
        or not current_scope.get("target")
        or not current_scope.get("profile_ref_fingerprint")
    ):
        store.add_event(
            checkpoint,
            "promotion_scope_incomplete",
            "缺少服务器目标或配置身份指纹，保留人工晋升",
        )
        return

    session = store.read_json(root / "data" / "evolution-session.json") or {}
    request_id = str(session.get("id", ""))
    if not request_id or not store.SAFE_ID.fullmatch(request_id):
        store.add_event(
            checkpoint,
            "promotion_authorization_failed",
            "无法识别 evolution request id",
        )
        return
    request_dir = root / "data" / "promote-requests" / request_id
    try:
        candidate_sha = (request_dir / "candidate.sha256").read_text(
            encoding="utf-8"
        ).strip()
    except OSError:
        candidate_sha = ""
    if not _SHA256.fullmatch(candidate_sha):
        store.add_event(
            checkpoint,
            "promotion_authorization_failed",
            "候选摘要不存在或格式非法",
        )
        return

    changed = changed_files(root)
    outside = sorted(set(changed) - DOCKER_SKILL_AUTO_PROMOTE_PATHS)
    if not changed or outside:
        detail = f"自动授权只允许 Docker 技能测试集；越界文件={outside or 'none'}"
        store.add_event(checkpoint, "promotion_scope_rejected", detail)
        return

    record = {
        "schema_version": 1,
        "kind": "owner_scoped_docker_skill_upgrade",
        "request_id": request_id,
        "checkpoint_id": checkpoint["id"],
        "statement_sha256": authorization.get("statement_sha256"),
        "candidate_sha256": candidate_sha,
        "changed_files": changed,
        "scope": {
            key: current_scope.get(key)
            for key in (
                "capability",
                "target",
                "container",
                "service",
                "project",
                "profile_ref_fingerprint",
                "ssh_identity_fingerprint",
            )
            if current_scope.get(key)
        },
        "allowed_paths": sorted(DOCKER_SKILL_AUTO_PROMOTE_PATHS),
        "does_not_bypass_external_approval": True,
        "created_at": store.now_iso(),
        "expires_at": (
            store.now() + timedelta(minutes=AUTH_TTL_MINUTES)
        ).isoformat(timespec="seconds"),
    }
    path = root / "data" / "promotion-authorizations" / f"{request_id}.json"
    store.atomic_json(path, record)
    checkpoint["promotion_authorization"] = {
        "request_id": request_id,
        "candidate_sha256": candidate_sha,
        "changed_files": changed,
        "expires_at": record["expires_at"],
    }
    store.add_event(
        checkpoint,
        "promotion_authorized",
        f"已绑定 exact candidate {candidate_sha}",
    )
