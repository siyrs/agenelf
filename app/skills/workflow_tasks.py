"""Governed long-running workflow task tools.

This skill mutates only Agenelf task records.  It never executes server commands,
code patches or model calls directly; those side effects remain in their existing
capability and approval control planes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from core.task_engine import TaskEngine, TaskEngineError

SKILL_META = {
    "name": "workflow_tasks",
    "description": "长期任务编排：状态机、步骤依赖、审批等待、暂停恢复、可信证据和完成门控。",
    "version": "1.0.0",
}

CAPABILITY_META = {
    "id": "agent.workflow",
    "name": "受治理长期任务编排",
    "description": "把主人目标保存为可恢复任务，组合运维、验证、代码和自我改进能力，但不直接获得执行特权。",
    "version": "1.0.0",
    "domain": "orchestration",
    "operations": [
        {"name": "create", "description": "创建带验收、证据和回滚计划的任务", "risk": "change"},
        {"name": "list", "description": "列出任务与进度", "risk": "read"},
        {"name": "get", "description": "读取完整任务", "risk": "read"},
        {"name": "transition", "description": "按状态机暂停、恢复、失败、取消或完成", "risk": "change"},
        {"name": "update_step", "description": "推进步骤并关联审批或可信证据", "risk": "change"},
        {"name": "add_evidence", "description": "关联运维、验证、测试或晋升证据", "risk": "change"},
        {"name": "next_action", "description": "计算下一个可执行或待审批动作", "risk": "read"},
    ],
    "composes_with": [
        "server.operations",
        "software.validation",
        "agent.self_development",
        "code.repair",
        "software.release",
    ],
}

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "workflow_create_task",
            "description": "创建长期任务。包含变更步骤时 rollback_plan 必填；该操作只创建任务记录，不执行步骤。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "owner_goal": {"type": "string"},
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "capability": {"type": "string"},
                                "operation": {"type": "string"},
                                "target": {"type": "string"},
                                "parameters_ref": {"type": "string"},
                                "risk": {
                                    "type": "string",
                                    "enum": ["read", "change", "privileged", "irreversible"],
                                },
                                "depends_on": {
                                    "type": "array",
                                    "items": {"type": "integer"},
                                },
                            },
                            "required": ["title"],
                        },
                    },
                    "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                    "evidence_plan": {"type": "array", "items": {"type": "string"}},
                    "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
                    "source_channel": {"type": "string"},
                    "rollback_plan": {"type": "string"},
                },
                "required": ["title", "owner_goal", "steps", "acceptance_criteria", "evidence_plan"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workflow_list_tasks",
            "description": "列出长期任务摘要，可按状态过滤。",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workflow_get_task",
            "description": "读取一个任务的完整状态、步骤、证据和审计事件。",
            "parameters": {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workflow_transition_task",
            "description": "按任务状态机进行启动、暂停、恢复、验证、完成、失败或取消。完成必须已有可信证据。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "status": {"type": "string"},
                    "reason": {"type": "string"},
                    "expected_revision": {"type": "integer"},
                },
                "required": ["task_id", "status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workflow_update_step",
            "description": "推进步骤状态。等待授权要关联 op-/auth- ID；成功要关联 evidence_reference。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "step_index": {"type": "integer", "minimum": 0},
                    "status": {"type": "string"},
                    "note": {"type": "string"},
                    "evidence_reference": {"type": "string"},
                    "evidence_kind": {"type": "string"},
                    "approval_request_id": {"type": "string"},
                    "expected_revision": {"type": "integer"},
                },
                "required": ["task_id", "step_index", "status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workflow_add_evidence",
            "description": "把运维、验证、测试、产物、审批、日志或晋升引用关联到任务。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "kind": {"type": "string"},
                    "reference": {"type": "string"},
                    "summary": {"type": "string"},
                    "step_index": {"type": "integer"},
                    "expected_revision": {"type": "integer"},
                },
                "required": ["task_id", "kind", "reference"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workflow_next_action",
            "description": "根据依赖、审批、失败和验证证据计算下一步，不执行任何副作用。",
            "parameters": {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
        },
    },
]


def _root() -> Path:
    configured = os.environ.get("AGENELF_ROOT", "").strip()
    return Path(configured).resolve() if configured else Path(__file__).resolve().parents[2]


def _engine() -> TaskEngine:
    return TaskEngine(_root())


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _optional_int(data: dict, key: str) -> int | None:
    return int(data[key]) if key in data and data[key] is not None else None


def execute(tool_name: str, args: dict) -> str:
    data = args or {}
    try:
        engine = _engine()
        if tool_name == "workflow_create_task":
            return _dump({"ok": True, "task": engine.create(
                title=str(data.get("title", "")),
                owner_goal=str(data.get("owner_goal", "")),
                steps=data.get("steps", []),
                acceptance_criteria=data.get("acceptance_criteria", []),
                evidence_plan=data.get("evidence_plan", []),
                priority=str(data.get("priority", "P2")),
                source_channel=str(data.get("source_channel", "chat")),
                rollback_plan=str(data.get("rollback_plan", "")),
            )})
        if tool_name == "workflow_list_tasks":
            values = engine.list_tasks(
                status=str(data.get("status", "")),
                limit=int(data.get("limit", 50) or 50),
            )
            return _dump({"ok": True, "tasks": values, "count": len(values)})
        if tool_name == "workflow_get_task":
            return _dump({"ok": True, "task": engine.get(str(data.get("task_id", "")))})
        if tool_name == "workflow_transition_task":
            task = engine.transition(
                str(data.get("task_id", "")),
                str(data.get("status", "")),
                reason=str(data.get("reason", "")),
                expected_revision=_optional_int(data, "expected_revision"),
            )
            return _dump({"ok": True, "task": task})
        if tool_name == "workflow_update_step":
            task = engine.update_step(
                str(data.get("task_id", "")),
                int(data.get("step_index", -1)),
                str(data.get("status", "")),
                note=str(data.get("note", "")),
                evidence_reference=str(data.get("evidence_reference", "")),
                evidence_kind=str(data.get("evidence_kind", "note")),
                approval_request_id=str(data.get("approval_request_id", "")),
                expected_revision=_optional_int(data, "expected_revision"),
            )
            return _dump({"ok": True, "task": task})
        if tool_name == "workflow_add_evidence":
            task = engine.add_evidence(
                str(data.get("task_id", "")),
                kind=str(data.get("kind", "")),
                reference=str(data.get("reference", "")),
                summary=str(data.get("summary", "")),
                step_index=_optional_int(data, "step_index"),
                expected_revision=_optional_int(data, "expected_revision"),
            )
            return _dump({"ok": True, "task": task})
        if tool_name == "workflow_next_action":
            return _dump({"ok": True, **engine.next_action(str(data.get("task_id", "")))})
        return _dump({"ok": False, "error": f"未知工具：{tool_name}"})
    except (TaskEngineError, TypeError, ValueError) as exc:
        return _dump({"ok": False, "error": str(exc)})
    except Exception as exc:
        return _dump({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
