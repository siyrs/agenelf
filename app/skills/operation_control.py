"""Read-only operation control and owner revocation guidance.

This skill intentionally exposes no cancellation function to the model.  It can list
requests that the owner may still revoke and produce exact host-side commands.  The
actual state change is performed only by ``scripts/revoke.py`` (or its PowerShell/Shell
wrappers), which competes with ``ops-runner`` on the same per-request execution lock.
"""
from __future__ import annotations

import json
from typing import Any

from core import operation_revocation

SKILL_META = {
    "name": "operation_control",
    "description": (
        "只读查看可撤销的运维请求、请求控制状态和跨平台主人撤销命令；"
        "模型本身不能撤销请求。"
    ),
    "version": "1.0.0",
}

CAPABILITY_META = {
    "id": "agent.operation_control",
    "name": "运维请求控制",
    "description": (
        "检查运维请求是否仍可在执行前撤销，并给出 Windows/跨平台主人命令；"
        "不写结果、不触发 SSH、不构成主人授权。"
    ),
    "version": "1.0.0",
    "domain": "operations",
    "operations": [
        {
            "name": "list_revocable_operations",
            "description": "列出尚未开始且仍可由主人原子撤销的运维请求",
            "risk": "read",
            "execution_mode": "pure",
        },
        {
            "name": "get_operation_control_status",
            "description": "查看一个请求的安全控制状态，不返回自由参数或 Compose 正文",
            "risk": "read",
            "execution_mode": "pure",
        },
        {
            "name": "get_operation_revocation_instructions",
            "description": "生成主人在 Windows、Python 或 Shell 中执行的精确撤销命令",
            "risk": "read",
            "execution_mode": "pure",
        },
    ],
    "composes_with": [
        "server.operations",
        "docker.operations",
        "docker.compose_lifecycle",
        "agent.runtime_doctor",
        "agent.task_continuation",
    ],
}

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_revocable_operations",
            "description": (
                "列出尚无结果、未过期且未被 Runner 取得执行锁的运维请求。"
                "这是只读清单；模型不能据此自行撤销。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200}
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_operation_control_status",
            "description": (
                "查看指定 op-* 的状态、目标、风险、有效期、是否正在执行及是否仍可撤销。"
                "不会返回 parameters、Compose YAML 或凭据。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"operation_id": {"type": "string"}},
                "required": ["operation_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_operation_revocation_instructions",
            "description": (
                "为一个仍可撤销的请求生成主人执行命令。实际撤销必须由主人在宿主机运行，"
                "模型输出或工具调用不能充当撤销决定。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"operation_id": {"type": "string"}},
                "required": ["operation_id"],
                "additionalProperties": False,
            },
        },
    },
]


def list_revocable_operations(limit: int = 50) -> list[dict[str, Any]]:
    return operation_revocation.list_revocable_operations(limit=limit)


def get_operation_control_status(operation_id: str) -> dict[str, Any]:
    return operation_revocation.operation_control_status(operation_id)


def get_operation_revocation_instructions(operation_id: str) -> dict[str, Any]:
    status = operation_revocation.operation_control_status(operation_id)
    if not status.get("revocable"):
        return {
            "status": "not_revocable",
            "operation": status,
            "reason": (
                "请求可能已完成、已过期、已拒绝或已被 Runner 取得执行锁；"
                "不能宣称撤销成功。"
            ),
        }
    return {
        "status": "owner_action_required",
        "operation": status,
        "instructions": operation_revocation.revocation_instructions(operation_id),
    }


_DISPATCH = {
    "list_revocable_operations": lambda args: list_revocable_operations(
        int(args.get("limit", 50))
    ),
    "get_operation_control_status": lambda args: get_operation_control_status(
        str(args.get("operation_id", ""))
    ),
    "get_operation_revocation_instructions": lambda args: get_operation_revocation_instructions(
        str(args.get("operation_id", ""))
    ),
}


def execute(tool_name: str, args: dict[str, Any]) -> str:
    handler = _DISPATCH.get(tool_name)
    if handler is None:
        return json.dumps(
            {"status": "failed", "error": f"未知工具：{tool_name}"},
            ensure_ascii=False,
        )
    try:
        return json.dumps(handler(args or {}), ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps(
            {"status": "failed", "error": f"{type(exc).__name__}: {exc}"},
            ensure_ascii=False,
            indent=2,
        )
