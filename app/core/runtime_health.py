"""Deterministic runtime diagnostics for Agenelf and its isolated runners."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DEFAULT_RUNNERS = (
    "ops-runner",
    "approval-runner",
    "self-upgrade-runner",
    "validation-runner",
    "repair-runner",
)
_TERMINAL_UPGRADE_STATES = {
    "succeeded",
    "restart_required",
    "denied",
    "cancelled",
    "expired",
    "failed",
    "rolled_back",
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat(timespec="seconds")


def runtime_root(root: str | Path | None = None) -> Path:
    if root is not None:
        return Path(root).resolve()
    configured = os.environ.get("AGENELF_ROOT", "").strip()
    return Path(configured).resolve() if configured else Path(__file__).resolve().parents[2]


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _parse_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _bounded_float(value: object, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _expected_runners(config: dict[str, Any] | None = None) -> tuple[str, ...]:
    runtime_cfg = config.get("runtime_health", {}) if isinstance(config, dict) else {}
    raw = runtime_cfg.get("expected_runners") if isinstance(runtime_cfg, dict) else None
    if not isinstance(raw, list):
        return DEFAULT_RUNNERS
    values: list[str] = []
    for item in raw:
        name = str(item or "").strip().lower()
        if name and name not in values:
            values.append(name)
    return tuple(values) or DEFAULT_RUNNERS


def runner_health(
    root: str | Path | None = None,
    *,
    expected: Iterable[str] | None = None,
    stale_after_seconds: float = 15.0,
    at: datetime | None = None,
) -> dict[str, Any]:
    base = runtime_root(root)
    directory = base / "data" / "runner-health"
    current = (at or now_utc()).astimezone(timezone.utc)
    threshold_default = _bounded_float(stale_after_seconds, 15.0, 2.0, 300.0)
    names = tuple(expected or DEFAULT_RUNNERS)
    rows: dict[str, dict[str, Any]] = {}

    for raw_name in names:
        name = str(raw_name).strip().lower()
        path = directory / f"{name}.json"
        heartbeat = _read_json(path)
        if heartbeat is None:
            rows[name] = {
                "runner": name,
                "health": "missing" if not path.exists() else "invalid",
                "healthy": False,
                "heartbeat_path": f"data/runner-health/{name}.json",
            }
            continue

        heartbeat_at = _parse_time(heartbeat.get("heartbeat_at"))
        expires_after = _bounded_float(
            heartbeat.get("expires_after_seconds"),
            threshold_default,
            2.0,
            300.0,
        )
        threshold = max(threshold_default, expires_after)
        age_seconds = (
            max(0.0, (current - heartbeat_at).total_seconds())
            if heartbeat_at is not None
            else None
        )
        raw_status = str(heartbeat.get("status", "unknown")).strip().lower()
        fresh = age_seconds is not None and age_seconds <= threshold
        healthy = raw_status in {"starting", "running"} and fresh
        if heartbeat_at is None:
            health = "invalid"
        elif not fresh:
            health = "stale"
        elif raw_status in {"failed", "stopped", "stopping"}:
            health = raw_status
        elif healthy:
            health = "healthy"
        else:
            health = "unknown"

        row = {
            "runner": name,
            "health": health,
            "healthy": healthy,
            "status": raw_status,
            "heartbeat_at": heartbeat.get("heartbeat_at"),
            "age_seconds": round(age_seconds, 2) if age_seconds is not None else None,
            "expires_after_seconds": expires_after,
            "sequence": heartbeat.get("sequence"),
            "runtime_source": str(heartbeat.get("runtime_source", "unknown"))[:64],
            "heartbeat_path": f"data/runner-health/{name}.json",
        }
        if heartbeat.get("exit_code") is not None:
            row["exit_code"] = heartbeat.get("exit_code")
        if heartbeat.get("error"):
            row["error"] = " ".join(str(heartbeat.get("error")).split())[:500]
        rows[name] = row

    healthy_count = sum(1 for row in rows.values() if row.get("healthy"))
    return {
        "expected": len(rows),
        "healthy": healthy_count,
        "unhealthy": len(rows) - healthy_count,
        "all_healthy": bool(rows) and healthy_count == len(rows),
        "runners": rows,
    }


def _count_files(path: Path, pattern: str) -> int:
    if not path.is_dir():
        return 0
    return sum(1 for _ in path.glob(pattern))


def _queue_snapshot(root: Path) -> dict[str, Any]:
    data = root / "data"
    pending_ops = 0
    operations = data / "ops-requests"
    results = data / "ops-results"
    if operations.is_dir():
        for path in operations.glob("op-*.json"):
            if not (results / path.name).is_file():
                pending_ops += 1

    pending_approvals = 0
    try:
        from core import approval_catalog

        pending_approvals = len(approval_catalog.list_pending_requests(root))
    except Exception:
        pending_approvals = -1

    sessions = data / "authorized-upgrades"
    active_upgrades = 0
    failed_upgrades = 0
    if sessions.is_dir():
        for path in sessions.glob("upgrade-*.json"):
            value = _read_json(path)
            if not value:
                continue
            status = str(value.get("status", ""))
            if status == "failed":
                failed_upgrades += 1
            if status not in _TERMINAL_UPGRADE_STATES:
                active_upgrades += 1

    return {
        "pending_operations": pending_ops,
        "pending_approvals": pending_approvals,
        "active_authorized_upgrades": active_upgrades,
        "failed_authorized_upgrades": failed_upgrades,
        "task_continuation_exists": (data / "continuations" / "active.json").is_file(),
    }


def _path_check(root: Path, relative: str, *, writable: bool = False) -> dict[str, Any]:
    path = root / relative
    exists = path.is_dir() if relative.endswith("/") else path.exists()
    result = {
        "path": relative.rstrip("/"),
        "exists": exists,
        "writable": bool(exists and os.access(path, os.W_OK)) if writable else None,
        "required_writable": writable,
    }
    result["healthy"] = bool(exists and (not writable or result["writable"]))
    return result


def diagnose(
    root: str | Path | None = None,
    *,
    registry: Any | None = None,
    config: dict[str, Any] | None = None,
    at: datetime | None = None,
) -> dict[str, Any]:
    """Return a secret-free, deterministic runtime diagnostic snapshot."""

    base = runtime_root(root)
    expected = _expected_runners(config)
    runtime_cfg = config.get("runtime_health", {}) if isinstance(config, dict) else {}
    stale_after = (
        runtime_cfg.get("stale_after_seconds", 15)
        if isinstance(runtime_cfg, dict)
        else 15
    )
    runners = runner_health(
        base,
        expected=expected,
        stale_after_seconds=stale_after,
        at=at,
    )

    path_checks = [
        _path_check(base, "app-fork/"),
        _path_check(base, "app-tmp/", writable=True),
        _path_check(base, "data/", writable=True),
        _path_check(base, "data/auth-decisions/"),
        _path_check(base, "data/runner-health/"),
        _path_check(base, "local/servers.yaml"),
    ]
    failed_paths = [item["path"] for item in path_checks if not item["healthy"]]

    registry_errors = {}
    if registry is not None:
        raw = getattr(registry, "errors", {})
        if isinstance(raw, dict):
            registry_errors = {
                str(name): str(error).splitlines()[-1][:500]
                for name, error in raw.items()
            }

    queue = _queue_snapshot(base)
    runtime_source = os.environ.get("AGENELF_RUNTIME_SOURCE", "unknown")[:64]
    recommendations: list[str] = []
    for name, row in runners["runners"].items():
        if not row.get("healthy"):
            recommendations.append(
                f"重新创建或检查 {name}；查看 docker compose logs --tail=100 {name}"
            )
    if failed_paths:
        recommendations.append("执行 scripts/init_local.py 并检查宿主机目录权限：" + ", ".join(failed_paths))
    if registry_errors:
        recommendations.append("执行 /skills 查看加载错误，修复后使用 /reload <技能名>")
    if runtime_source not in {"app-bind", "unknown"}:
        recommendations.append("当前运行时代码来源不是 app-bind；重新创建 Agenelf 容器")
    if queue["failed_authorized_upgrades"]:
        recommendations.append("执行 /upgrade status 查看最近失败证据并在相同授权范围内有界重试")

    healthy = (
        runners["all_healthy"]
        and not failed_paths
        and not registry_errors
        and runtime_source in {"app-bind", "unknown"}
    )
    return {
        "schema_version": 1,
        "generated_at": (at or now_utc()).astimezone(timezone.utc).isoformat(timespec="seconds"),
        "status": "healthy" if healthy else "degraded",
        "summary": (
            f"Runner {runners['healthy']}/{runners['expected']} 健康；"
            f"路径异常 {len(failed_paths)}；技能错误 {len(registry_errors)}"
        ),
        "runtime_source": runtime_source,
        "runners": runners,
        "paths": path_checks,
        "registry_errors": registry_errors,
        "queues": queue,
        "recommendations": recommendations,
    }
