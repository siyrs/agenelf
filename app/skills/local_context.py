"""Owner personalization capability backed by root/local and local/memory."""

from __future__ import annotations

import json
from typing import Any

SKILL_META = {
    "name": "local_context",
    "description": "读取主人个性化配置、刷新 local 上下文，并安全保存/检索脱敏记忆。",
    "version": "1.0.0",
}

CAPABILITY_META = {
    "id": "owner.context",
    "name": "主人个性化上下文",
    "description": "主人画像、兴趣偏好、补充资料、服务器别名和脱敏长期记忆。",
    "version": "1.0.0",
    "domain": "personalization",
    "composes_with": ["agent.self_reflection", "server.operations"],
    "operations": [
        {"name": "status", "description": "查看 local 配置加载状态", "risk": "read"},
        {"name": "reload", "description": "重新加载 local 安全上下文", "risk": "read"},
        {"name": "remember", "description": "保存脱敏事实或偏好", "risk": "change"},
        {"name": "recall", "description": "检索主人长期记忆", "risk": "read"},
    ],
}

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_local_context_status",
            "description": "查看 local/ 个性化配置是否加载、服务器别名、警告和记忆统计；不返回凭据。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reload_local_context",
            "description": "主人修改 local/ 文件后重新加载，无需重启 Agent。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember_owner_context",
            "description": "将主人明确要求记住的事实或偏好脱敏后保存到 local/memory。",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["fact", "preference"]},
                    "content": {"type": "string"},
                },
                "required": ["kind", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_owner_context",
            "description": "按关键词检索主人长期记忆。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                },
                "required": ["query"],
            },
        },
    },
]

_agent = None


def configure_runtime(*, agent=None, **_: Any) -> None:
    global _agent
    _agent = agent


def _require_agent():
    if _agent is None:
        raise RuntimeError("local_context 技能尚未绑定 Agent 运行时")
    return _agent


def execute(tool_name: str, args: dict) -> str:
    try:
        agent = _require_agent()
        args = args or {}
        if tool_name == "get_local_context_status":
            return json.dumps(agent.local_status(), ensure_ascii=False, indent=2)
        if tool_name == "reload_local_context":
            return json.dumps(agent.reload_local_context(), ensure_ascii=False, indent=2)
        if tool_name == "remember_owner_context":
            kind = str(args.get("kind", "")).strip()
            content = str(args.get("content", "")).strip()
            if kind not in {"fact", "preference"}:
                return "保存失败：kind 只能是 fact 或 preference"
            if not content:
                return "保存失败：content 不能为空"
            return json.dumps(agent.remember_owner(kind, content), ensure_ascii=False, indent=2)
        if tool_name == "recall_owner_context":
            query = str(args.get("query", "")).strip()
            if not query:
                return "检索失败：query 不能为空"
            limit = min(20, max(1, int(args.get("limit", 5))))
            return json.dumps(
                {"query": query, "results": agent.recall_owner(query, limit)},
                ensure_ascii=False,
                indent=2,
            )
        return f"未知工具：{tool_name}"
    except Exception as exc:
        return f"执行失败：{type(exc).__name__}: {exc}"
