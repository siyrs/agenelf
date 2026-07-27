"""Pi-inspired append-only session ledger skill."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from core.session_ledger import SessionLedgerError, SessionLedgerStore

SKILL_META = {
    "name": "session_ledger",
    "description": "可分支、可回放、带哈希链的主人本地 Session Event Ledger。",
    "version": "0.1.0",
}

CAPABILITY_META = {
    "id": "agent.session_ledger",
    "description": (
        "以 append-only JSONL 保存结构化会话、检查点、反思和标签；"
        "parent_id 形成树，哈希链提供篡改检测，origin 明确事件来源。"
    ),
    "composes_with": [
        "agent.workflow",
        "agent.task_continuation",
        "agent.self_development",
        "software.validation",
        "code.repair",
    ],
    "operations": [
        {
            "name": "session_ledger_status",
            "risk": "read",
            "execution_mode": "pure",
            "description": "查看指定 session ledger 的完整性与分支摘要。",
        },
        {
            "name": "session_ledger_list",
            "risk": "read",
            "execution_mode": "pure",
            "description": "按类型、分支和数量列出 ledger entries。",
        },
        {
            "name": "session_ledger_get",
            "risk": "read",
            "execution_mode": "pure",
            "description": "读取指定 ledger entry。",
        },
        {
            "name": "session_ledger_append",
            "risk": "change",
            "execution_mode": "local_state",
            "description": "向主人本地 ledger 追加一条低信任、已脱敏事件。",
        },
        {
            "name": "session_ledger_branch",
            "risk": "change",
            "execution_mode": "local_state",
            "description": "从既有 entry 创建新分支并追加 branch_summary。",
        },
        {
            "name": "session_ledger_verify",
            "risk": "read",
            "execution_mode": "pure",
            "description": "验证序号、父节点、来源、哈希链和 entry hash。",
        },
    ],
}

# 模型可调用入口只允许写低信任的主人/Agent 叙事状态。tool/approval/runner/evidence
# 等安全关键事件必须由运行时或可信 Runner 自动写入，不能靠模型自述获得可信度。
MODEL_WRITABLE_EVENT_TYPES = [
    "message",
    "checkpoint",
    "reflection",
    "intention",
    "label",
    "custom",
]

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "session_ledger_status",
            "description": "查看指定 session 的 ledger 状态与完整性摘要。",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "会话 ID，1-64 位安全字符。",
                    }
                },
                "required": ["session_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "session_ledger_list",
            "description": "列出指定 session 的最近 ledger entries。",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "limit": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 500,
                        "default": 50,
                    },
                    "event_type": {
                        "type": "string",
                        "description": "可选事件类型过滤。",
                    },
                    "branch_id": {
                        "type": "string",
                        "description": "可选 main 或 br-xxxxxxxxxxxx。",
                    },
                },
                "required": ["session_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "session_ledger_get",
            "description": "读取一个精确 ledger entry。",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "entry_id": {"type": "string"},
                },
                "required": ["session_id", "entry_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "session_ledger_append",
            "description": (
                "向 session ledger 追加低信任结构化事件；payload 会递归脱敏。"
                "审批、Runner、工具结果和证据事件只能由运行时自动记录。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "event_type": {
                        "type": "string",
                        "enum": MODEL_WRITABLE_EVENT_TYPES,
                    },
                    "payload": {"type": "object"},
                    "parent_id": {
                        "type": "string",
                        "description": "可选；缺省自动挂到最后 entry。",
                    },
                    "branch_id": {
                        "type": "string",
                        "description": "可选；缺省继承 parent 分支。",
                    },
                },
                "required": ["session_id", "event_type", "payload"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "session_ledger_branch",
            "description": "从既有 entry 创建一个低信任叙事分支。",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "parent_id": {"type": "string"},
                    "label": {"type": "string", "minLength": 1, "maxLength": 200},
                    "summary": {"type": "string", "maxLength": 4000},
                },
                "required": ["session_id", "parent_id", "label"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "session_ledger_verify",
            "description": "验证指定 session ledger 的树引用、来源和哈希链。",
            "parameters": {
                "type": "object",
                "properties": {"session_id": {"type": "string"}},
                "required": ["session_id"],
                "additionalProperties": False,
            },
        },
    },
]


def _runtime_root() -> Path:
    configured = os.environ.get("AGENELF_ROOT", "").strip()
    return Path(configured).resolve() if configured else Path(__file__).resolve().parents[2]


def _store() -> SessionLedgerStore:
    return SessionLedgerStore(_runtime_root())


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def execute(tool_name: str, args: dict[str, Any]) -> str:
    data = args if isinstance(args, dict) else {}
    try:
        if tool_name == "session_ledger_status":
            return _json(_store().status(str(data.get("session_id", ""))))
        if tool_name == "session_ledger_list":
            return _json(
                {
                    "session_id": str(data.get("session_id", "")),
                    "entries": _store().entries(
                        str(data.get("session_id", "")),
                        limit=data.get("limit", 50),
                        event_type=str(data.get("event_type", "")),
                        branch_id=str(data.get("branch_id", "")),
                    ),
                }
            )
        if tool_name == "session_ledger_get":
            return _json(
                _store().get(
                    str(data.get("session_id", "")),
                    str(data.get("entry_id", "")),
                )
            )
        if tool_name == "session_ledger_append":
            event_type = str(data.get("event_type", ""))
            if event_type not in MODEL_WRITABLE_EVENT_TYPES:
                raise SessionLedgerError(
                    "模型工具不能写入安全关键事件类型；请由运行时或可信 Runner 记录"
                )
            return _json(
                _store().append(
                    str(data.get("session_id", "")),
                    event_type,
                    data.get("payload", {}),
                    parent_id=str(data.get("parent_id", "")).strip() or None,
                    branch_id=str(data.get("branch_id", "")).strip() or None,
                    origin="agent_skill",
                )
            )
        if tool_name == "session_ledger_branch":
            return _json(
                _store().create_branch(
                    str(data.get("session_id", "")),
                    str(data.get("parent_id", "")),
                    label=str(data.get("label", "")),
                    summary=str(data.get("summary", "")),
                    origin="agent_skill",
                )
            )
        if tool_name == "session_ledger_verify":
            return _json(_store().verify(str(data.get("session_id", ""))))
        return _json({"error": f"未知 session ledger 工具：{tool_name}"})
    except SessionLedgerError as exc:
        return _json({"error": str(exc), "type": type(exc).__name__})
