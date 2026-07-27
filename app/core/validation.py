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

from core.storage import atomic_write_json as _atomic_json
from core.storage import read_json as _read_storage_json

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


def _policy_evaluation(operation: str) -> dict[str, Any] | None:
    """咨询策略引擎；引擎不可用或调用失败时返回 None（降级为既有行为）。"""

    try:
        from core.policy import PolicyEngine  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        engine = PolicyEngine()
        if getattr(engine, "degraded", False):
            # 策略文件缺失/损坏 → 视为引擎不可用，回退既有行为；
            # 只有健康引擎的判定才具有约束力（兼容未部署 policy/ 的环境）
            return None
        result = engine.evaluate(_CAPABILITY, operation, subject="agent")
    except Exception:
        return None
    return result if isinstance(result, dict) else None


def read_json(path: Path) -> dict[str, Any] | None:
    value = _read_storage_json(path)
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
    evaluation = _policy_evaluation(payload["operation"])
    if evaluation:
        approval_mode = str(evaluation.get("approval") or "")
        if evaluation.get("allowed") is False or approval_mode == "impossible":
            reason = str(evaluation.get("reason") or "策略禁止")
            raise PermissionError(f"策略引擎拒绝提交验证：{reason}")
        policy_risk = str(evaluation.get("risk") or "").lower().strip()
        if policy_risk and policy_risk != "read":
            # 验证 Runner 只自动执行 read 级请求；策略判定更严格时失败关闭。
            raise PermissionError(
                f"策略引擎判定验证风险为 {policy_risk}，超出自动执行范围，拒绝提交"
            )
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
    if evaluation:
        request["policy_version"] = str(evaluation.get("policy_version") or "")
        request["approval_mode"] = str(evaluation.get("approval") or "")
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
