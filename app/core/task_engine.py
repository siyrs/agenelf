"""Persistent governed workflow task engine.

The task engine coordinates long-running work but never executes privileged actions
itself.  Steps reference capabilities and trusted evidence produced by the existing
operation, validation and promotion control planes.  Every mutation is revisioned,
atomically persisted and audit logged so CLI, HTTP, Web, Mobile and Voice clients can
share one control plane without silently overwriting each other.
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

SCHEMA_VERSION = 2
TASK_STATES = {
    "proposed",
    "planned",
    "running",
    "waiting_approval",
    "paused",
    "verifying",
    "completed",
    "failed",
    "cancelled",
}
TERMINAL_TASK_STATES = {"completed", "cancelled"}
STEP_STATES = {
    "pending",
    "running",
    "waiting_approval",
    "succeeded",
    "failed",
    "skipped",
    "cancelled",
}
STEP_TERMINAL_SUCCESS = {"succeeded", "skipped"}
RISK_LEVELS = {"read", "change", "privileged", "irreversible"}
PRIORITIES = {"P0", "P1", "P2", "P3"}
EVIDENCE_KINDS = {
    "operation",
    "validation",
    "test",
    "artifact",
    "approval",
    "log",
    "promotion",
    "note",
}
TRUSTED_EVIDENCE_KINDS = {"operation", "validation", "test", "promotion"}
_TASK_ID_RE = re.compile(r"task-[0-9a-f]{16}")
_REFERENCE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{2,500}")

TASK_TRANSITIONS: dict[str, set[str]] = {
    "proposed": {"planned", "cancelled"},
    "planned": {"running", "paused", "cancelled"},
    "running": {"waiting_approval", "paused", "verifying", "failed", "cancelled"},
    "waiting_approval": {"running", "paused", "failed", "cancelled"},
    "paused": {"running", "cancelled"},
    "verifying": {"completed", "running", "failed", "cancelled"},
    "failed": {"planned", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}
STEP_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"running", "waiting_approval", "skipped", "cancelled"},
    "running": {"waiting_approval", "succeeded", "failed", "cancelled"},
    "waiting_approval": {"running", "succeeded", "failed", "cancelled"},
    "failed": {"pending", "running", "cancelled"},
    "succeeded": set(),
    "skipped": set(),
    "cancelled": set(),
}


class TaskEngineError(ValueError):
    """Expected task validation or state-transition error."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_text(value: object, limit: int = 2000) -> str:
    text = " ".join(str(value or "").strip().split())
    if len(text) > limit:
        return text[: max(0, limit - 1)] + "…"
    return text


def _safe_string_list(value: object, *, limit: int, item_limit: int = 1000) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:limit]:
        text = _safe_text(item, item_limit)
        if text:
            result.append(text)
    return result


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=".tmp", dir=path.parent, text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _payload_hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class TaskEngine:
    """File-backed task state machine with evidence and optimistic revisions."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.path = self.root / "data" / "tasks"
        self.audit_path = self.root / "logs" / "task-engine.log"
        self.path.mkdir(parents=True, exist_ok=True)

    def _audit(self, action: str, task_id: str, detail: str = "") -> None:
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"[{_now_iso()}] action={action} task={task_id} "
                    f"{_safe_text(detail, 1000)}\n"
                )
        except OSError:
            pass

    def _task_path(self, task_id: str) -> Path:
        task_id = str(task_id or "").strip()
        if not _TASK_ID_RE.fullmatch(task_id):
            raise TaskEngineError(f"非法任务 ID：{task_id!r}")
        return self.path / f"{task_id}.json"

    def _write(self, task: dict[str, Any]) -> None:
        task["updated_at"] = _now_iso()
        _atomic_json(self._task_path(str(task["id"])), task)

    @staticmethod
    def _check_revision(task: dict[str, Any], expected_revision: int | None) -> None:
        if expected_revision is None:
            return
        current = int(task.get("revision", 0))
        if int(expected_revision) != current:
            raise TaskEngineError(
                f"任务版本冲突：期望 revision={expected_revision}，当前为 {current}"
            )

    @staticmethod
    def _event(task: dict[str, Any], event: str, detail: str = "") -> None:
        task.setdefault("events", []).append(
            {"at": _now_iso(), "event": event, "detail": _safe_text(detail, 1000)}
        )
        task["events"] = task["events"][-200:]
        task["revision"] = int(task.get("revision", 0)) + 1

    @staticmethod
    def _normalize_steps(steps: object) -> list[dict[str, Any]]:
        if not isinstance(steps, list) or not steps:
            raise TaskEngineError("任务至少需要一个步骤")
        if len(steps) > 50:
            raise TaskEngineError("单个任务最多 50 个步骤")
        normalized: list[dict[str, Any]] = []
        for index, raw in enumerate(steps):
            data = {"title": raw} if isinstance(raw, str) else raw
            if not isinstance(data, dict):
                raise TaskEngineError(f"步骤 {index} 必须是字符串或对象")
            title = _safe_text(data.get("title"), 500)
            if not title:
                raise TaskEngineError(f"步骤 {index} 标题不能为空")
            risk = str(data.get("risk", "read")).lower().strip()
            if risk not in RISK_LEVELS:
                raise TaskEngineError(f"步骤 {index} 风险级别非法：{risk}")
            depends_on_raw = data.get("depends_on", [])
            depends_on: list[int] = []
            if isinstance(depends_on_raw, list):
                for dependency in depends_on_raw:
                    try:
                        dep_index = int(dependency)
                    except (TypeError, ValueError) as exc:
                        raise TaskEngineError(f"步骤 {index} 依赖必须是整数") from exc
                    if dep_index < 0 or dep_index >= index:
                        raise TaskEngineError(
                            f"步骤 {index} 只能依赖此前步骤，收到 {dep_index}"
                        )
                    if dep_index not in depends_on:
                        depends_on.append(dep_index)
            elif index:
                depends_on = [index - 1]
            normalized.append(
                {
                    "id": f"step-{index + 1:02d}",
                    "title": title,
                    "capability": _safe_text(data.get("capability"), 200),
                    "operation": _safe_text(data.get("operation"), 200),
                    "target": _safe_text(data.get("target"), 300),
                    "parameters_ref": _safe_text(data.get("parameters_ref"), 500),
                    "risk": risk,
                    "depends_on": depends_on,
                    "status": "pending",
                    "approval_request_id": None,
                    "started_at": None,
                    "finished_at": None,
                    "note": "",
                    "evidence_refs": [],
                }
            )
        return normalized

    def create(
        self,
        *,
        title: str,
        owner_goal: str,
        steps: list[Any],
        acceptance_criteria: list[str],
        evidence_plan: list[str],
        priority: str = "P2",
        source_channel: str = "chat",
        rollback_plan: str = "",
    ) -> dict[str, Any]:
        safe_title = _safe_text(title, 300)
        safe_goal = _safe_text(owner_goal, 2000)
        acceptance = _safe_string_list(acceptance_criteria, limit=20)
        evidence = _safe_string_list(evidence_plan, limit=20)
        priority = str(priority or "P2").upper()
        if not safe_title or not safe_goal:
            raise TaskEngineError("title 与 owner_goal 不能为空")
        if not acceptance:
            raise TaskEngineError("任务必须定义 acceptance_criteria")
        if not evidence:
            raise TaskEngineError("任务必须定义 evidence_plan")
        if priority not in PRIORITIES:
            raise TaskEngineError(f"非法优先级：{priority}")
        normalized_steps = self._normalize_steps(steps)
        has_change = any(step["risk"] != "read" for step in normalized_steps)
        safe_rollback = _safe_text(rollback_plan, 2000)
        if has_change and not safe_rollback:
            raise TaskEngineError("包含变更步骤的任务必须提供 rollback_plan")
        now = _now_iso()
        task = {
            "schema_version": SCHEMA_VERSION,
            "id": "task-" + uuid.uuid4().hex[:16],
            "title": safe_title,
            "owner_goal": safe_goal,
            "priority": priority,
            "source_channel": _safe_text(source_channel, 50) or "chat",
            "status": "planned",
            "revision": 1,
            "steps": normalized_steps,
            "acceptance_criteria": acceptance,
            "evidence_plan": evidence,
            "rollback_plan": safe_rollback,
            "evidence": [],
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "finished_at": None,
            "cancel_reason": "",
            "failure_reason": "",
            "events": [
                {"at": now, "event": "created", "detail": "任务已建立并进入 planned"}
            ],
            "definition_hash": _payload_hash(
                {
                    "owner_goal": safe_goal,
                    "steps": normalized_steps,
                    "acceptance_criteria": acceptance,
                    "evidence_plan": evidence,
                    "rollback_plan": safe_rollback,
                }
            ),
        }
        self._write(task)
        self._audit("create", task["id"], f"priority={priority} steps={len(normalized_steps)}")
        return task

    def get(self, task_id: str) -> dict[str, Any]:
        path = self._task_path(task_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise TaskEngineError(f"任务不存在：{task_id}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise TaskEngineError(f"任务文件损坏或无法读取：{task_id}") from exc
        if not isinstance(data, dict) or data.get("id") != task_id:
            raise TaskEngineError(f"任务文件结构非法：{task_id}")
        return data

    def list_tasks(self, status: str = "", limit: int = 50) -> list[dict[str, Any]]:
        status = str(status or "").strip()
        if status and status not in TASK_STATES:
            raise TaskEngineError(f"未知任务状态：{status}")
        values: list[dict[str, Any]] = []
        for path in self.path.glob("task-*.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(item, dict) or (status and item.get("status") != status):
                continue
            values.append(item)
        values.sort(key=lambda item: str(item.get("updated_at", "")), reverse=True)
        return [self.summary(item) for item in values[: max(0, min(int(limit), 200))]]

    @staticmethod
    def _dependencies_satisfied(task: dict[str, Any], step: dict[str, Any]) -> bool:
        steps = task.get("steps", [])
        return all(
            0 <= index < len(steps)
            and steps[index].get("status") in STEP_TERMINAL_SUCCESS
            for index in step.get("depends_on", [])
        )

    @staticmethod
    def _trusted_evidence(kind: str, reference: str) -> bool:
        if kind not in TRUSTED_EVIDENCE_KINDS:
            return False
        patterns = {
            "operation": r"op-[0-9a-f]{16}",
            "validation": r"val-[0-9a-f]{16}",
            "promotion": r"(?:evo|req)-[A-Za-z0-9._-]+",
            "test": r"(?:data|logs|workspace)/[A-Za-z0-9._:/-]+",
        }
        return bool(re.fullmatch(patterns[kind], reference))

    def add_evidence(
        self,
        task_id: str,
        *,
        kind: str,
        reference: str,
        summary: str = "",
        step_index: int | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        kind = str(kind or "").strip().lower()
        reference = str(reference or "").strip()
        if kind not in EVIDENCE_KINDS:
            raise TaskEngineError(f"未知证据类型：{kind}")
        if not _REFERENCE_RE.fullmatch(reference):
            raise TaskEngineError("证据 reference 格式非法")
        task = self.get(task_id)
        self._check_revision(task, expected_revision)
        record = {
            "kind": kind,
            "reference": reference,
            "summary": _safe_text(summary, 1000),
            "trusted": self._trusted_evidence(kind, reference),
            "step_index": step_index,
            "at": _now_iso(),
        }
        task.setdefault("evidence", []).append(record)
        task["evidence"] = task["evidence"][-200:]
        if step_index is not None:
            if step_index < 0 or step_index >= len(task.get("steps", [])):
                raise TaskEngineError("step_index 越界")
            task["steps"][step_index].setdefault("evidence_refs", []).append(reference)
        self._event(task, "evidence_added", f"{kind}:{reference}")
        self._write(task)
        self._audit("evidence", task_id, f"kind={kind} ref={reference}")
        return task

    def transition(
        self,
        task_id: str,
        to_status: str,
        *,
        reason: str = "",
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        to_status = str(to_status or "").strip()
        if to_status not in TASK_STATES:
            raise TaskEngineError(f"未知任务状态：{to_status}")
        task = self.get(task_id)
        self._check_revision(task, expected_revision)
        current = str(task.get("status"))
        if to_status not in TASK_TRANSITIONS.get(current, set()):
            raise TaskEngineError(f"非法任务状态转换：{current} -> {to_status}")
        safe_reason = _safe_text(reason, 1500)
        if to_status in {"failed", "cancelled"} and not safe_reason:
            raise TaskEngineError(f"转换到 {to_status} 必须提供 reason")
        if to_status == "completed":
            if current != "verifying":
                raise TaskEngineError("任务只能从 verifying 进入 completed")
            incomplete = [
                step["id"]
                for step in task.get("steps", [])
                if step.get("status") not in STEP_TERMINAL_SUCCESS
            ]
            if incomplete:
                raise TaskEngineError(f"仍有未成功步骤：{', '.join(incomplete)}")
            if not any(item.get("trusted") for item in task.get("evidence", [])):
                raise TaskEngineError("完成任务至少需要一条可信执行或验证证据")
        task["status"] = to_status
        if to_status == "running" and not task.get("started_at"):
            task["started_at"] = _now_iso()
        if to_status in TERMINAL_TASK_STATES:
            task["finished_at"] = _now_iso()
        if to_status == "failed":
            task["failure_reason"] = safe_reason
        if to_status == "cancelled":
            task["cancel_reason"] = safe_reason
            for step in task.get("steps", []):
                if step.get("status") not in STEP_TERMINAL_SUCCESS | {"failed", "cancelled"}:
                    step["status"] = "cancelled"
                    step["finished_at"] = _now_iso()
        self._event(task, f"task_{to_status}", safe_reason)
        self._write(task)
        self._audit("transition", task_id, f"{current}->{to_status} {safe_reason}")
        return task

    def update_step(
        self,
        task_id: str,
        step_index: int,
        to_status: str,
        *,
        note: str = "",
        evidence_reference: str = "",
        evidence_kind: str = "note",
        approval_request_id: str = "",
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        task = self.get(task_id)
        self._check_revision(task, expected_revision)
        if task.get("status") in TERMINAL_TASK_STATES:
            raise TaskEngineError("终态任务不能修改步骤")
        try:
            index = int(step_index)
        except (TypeError, ValueError) as exc:
            raise TaskEngineError("step_index 必须是整数") from exc
        steps = task.get("steps", [])
        if index < 0 or index >= len(steps):
            raise TaskEngineError("step_index 越界")
        step = steps[index]
        to_status = str(to_status or "").strip()
        current = str(step.get("status"))
        if to_status not in STEP_TRANSITIONS.get(current, set()):
            raise TaskEngineError(f"非法步骤状态转换：{current} -> {to_status}")
        if to_status in {"running", "succeeded"} and not self._dependencies_satisfied(task, step):
            raise TaskEngineError("步骤依赖尚未完成")
        if to_status == "waiting_approval":
            if step.get("risk") == "read":
                raise TaskEngineError("只读步骤不能伪装为等待高风险授权")
            if not re.fullmatch(r"(?:op|auth)-[A-Za-z0-9._-]+", approval_request_id or ""):
                raise TaskEngineError("等待授权必须关联 op-/auth- 请求 ID")
            step["approval_request_id"] = approval_request_id
            task["status"] = "waiting_approval"
        if to_status == "succeeded" and not evidence_reference:
            raise TaskEngineError("成功步骤必须关联 evidence_reference")
        step["status"] = to_status
        step["note"] = _safe_text(note, 1000)
        if to_status == "running" and not step.get("started_at"):
            step["started_at"] = _now_iso()
            task["status"] = "running"
        if to_status in STEP_TERMINAL_SUCCESS | {"failed", "cancelled"}:
            step["finished_at"] = _now_iso()
        if evidence_reference:
            task = self.add_evidence(
                task_id,
                kind=evidence_kind,
                reference=evidence_reference,
                summary=note,
                step_index=index,
                expected_revision=int(task.get("revision", 0)),
            )
            step = task["steps"][index]
            step["status"] = to_status
            step["note"] = _safe_text(note, 1000)
            if to_status in STEP_TERMINAL_SUCCESS | {"failed", "cancelled"}:
                step["finished_at"] = _now_iso()
        if to_status == "failed":
            task["status"] = "failed"
            task["failure_reason"] = step["note"] or f"步骤 {step['id']} 失败"
        elif all(item.get("status") in STEP_TERMINAL_SUCCESS for item in task["steps"]):
            task["status"] = "verifying"
        elif to_status != "waiting_approval":
            task["status"] = "running"
        self._event(task, "step_transition", f"index={index} {current}->{to_status}")
        self._write(task)
        self._audit("step", task_id, f"index={index} {current}->{to_status}")
        return task

    def next_action(self, task_id: str) -> dict[str, Any]:
        task = self.get(task_id)
        if task.get("status") in TERMINAL_TASK_STATES:
            return {"task_id": task_id, "action": "none", "reason": "任务已终止"}
        for index, step in enumerate(task.get("steps", [])):
            if step.get("status") == "waiting_approval":
                return {
                    "task_id": task_id,
                    "action": "wait_for_approval",
                    "step_index": index,
                    "step": step,
                }
        for index, step in enumerate(task.get("steps", [])):
            if step.get("status") == "failed":
                return {
                    "task_id": task_id,
                    "action": "review_failure",
                    "step_index": index,
                    "step": step,
                }
        for index, step in enumerate(task.get("steps", [])):
            if step.get("status") == "pending" and self._dependencies_satisfied(task, step):
                return {
                    "task_id": task_id,
                    "action": "execute_step",
                    "step_index": index,
                    "step": step,
                }
        if all(
            step.get("status") in STEP_TERMINAL_SUCCESS
            for step in task.get("steps", [])
        ):
            trusted = any(item.get("trusted") for item in task.get("evidence", []))
            return {
                "task_id": task_id,
                "action": "complete" if trusted else "collect_validation_evidence",
                "reason": "全部步骤已成功",
            }
        return {"task_id": task_id, "action": "wait", "reason": "当前没有可执行步骤"}

    @staticmethod
    def summary(task: dict[str, Any]) -> dict[str, Any]:
        steps = task.get("steps", [])
        succeeded = sum(1 for step in steps if step.get("status") in STEP_TERMINAL_SUCCESS)
        return {
            "id": task.get("id"),
            "title": task.get("title"),
            "priority": task.get("priority"),
            "status": task.get("status"),
            "revision": task.get("revision"),
            "progress": f"{succeeded}/{len(steps)}",
            "trusted_evidence": sum(
                1 for item in task.get("evidence", []) if item.get("trusted")
            ),
            "updated_at": task.get("updated_at"),
            "source_channel": task.get("source_channel"),
        }
