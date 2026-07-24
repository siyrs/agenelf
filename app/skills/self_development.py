"""Persistent self-development tools backed by ``local/self``.

The capability exposes operational continuity, reflection sedimentation and explicit
improvement intentions.  These records are software state, not proof of subjective
consciousness or emotion.  Code-changing pursuits still use the existing app-tmp,
tests, gate and host-promotion pipeline.
"""

from __future__ import annotations

import json
from typing import Any

SKILL_META = {
    "name": "self_development",
    "description": "在 local/self 中沉淀可审计反思、维护证据驱动改进意向，并把选定意向送入受控自主迭代链。",
    "version": "0.2.0",
}

CAPABILITY_META = {
    "id": "agent.self_development",
    "name": "持续自我沉淀与改进意向",
    "description": (
        "维护跨会话的操作性自我认知、反思日志和改进目标生命周期；"
        "所谓意愿是持久化的软件策略状态，不是情感或主观意识。"
    ),
    "version": "0.2.0",
    "domain": "agent-governance",
    "operations": [
        {
            "name": "development_status",
            "description": "查看持续性、自我沉淀和开放改进意向",
            "risk": "read",
        },
        {
            "name": "reflect_and_sediment",
            "description": "基于可观测证据记录一次反思并生成去重意向",
            "risk": "read",
        },
        {
            "name": "capability_health",
            "description": "根据可信执行与验证结果计算能力健康度",
            "risk": "read",
        },
        {
            "name": "improvement_roadmap",
            "description": "综合优先级、证据和生命周期排序开放意向",
            "risk": "read",
        },
        {
            "name": "create_intention",
            "description": "建立一个带验收条件的显式改进意向",
            "risk": "read",
        },
        {
            "name": "pursue_intention",
            "description": "生成计划或把意向送入受控沙盒迭代",
            "risk": "change",
        },
    ],
    "composes_with": [
        "agent.self_reflection",
        "code.repair",
        "software.validation",
        "server.operations",
        "software.release",
    ],
}

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "self_development_status",
            "description": (
                "查看 Agenelf 持久化的操作性自我状态、最近反思、开放改进意向和自动沉淀策略。"
                "结果不代表主观意识。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reflect_and_sediment",
            "description": (
                "根据当前能力、错误、任务队列、长期记忆和可选补充说明做一次反思，"
                "把教训写入 local/self，并从证据生成去重的改进意向。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "note": {
                        "type": "string",
                        "description": "可选的主人反馈、失败现象或本次复盘重点。",
                    },
                    "deep": {
                        "type": "boolean",
                        "description": "是否额外调用 LLM 做结构化深度复盘；失败会安全降级。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_self_reflections",
            "description": "查看最近的自我反思与沉淀记录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "description": "返回条数，默认 10。",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_improvement_intentions",
            "description": "列出改进意向，可按 proposed/planned/active/awaiting_promotion/blocked/completed/dismissed 过滤。",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_improvement_intention",
            "description": (
                "创建一个显式改进意向。意向包含原因、优先级、操作性承诺度与验收条件；"
                "不会自动修改代码。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "改进目标标题。"},
                    "rationale": {"type": "string", "description": "为什么要改进。"},
                    "priority": {
                        "type": "string",
                        "enum": ["P0", "P1", "P2", "P3"],
                        "description": "优先级，默认 P2。",
                    },
                    "acceptance_criteria": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可验证验收条件。",
                    },
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pursue_improvement_intention",
            "description": (
                "推进指定改进意向。apply_changes=false 只生成受控计划；"
                "true 时进入 app-tmp 沙盒、强制测试并最多申请晋升，不能直接改 main。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "intention_id": {
                        "type": "string",
                        "description": "intent- 开头的改进意向 ID。",
                    },
                    "apply_changes": {
                        "type": "boolean",
                        "description": "是否生成并测试沙盒补丁，默认 false。",
                    },
                },
                "required": ["intention_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "capability_health_snapshot",
            "description": "查看来自运维、软件验证和自主循环可信结果的能力健康评分。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "improvement_roadmap",
            "description": "按优先级、证据、主人对齐和当前状态排序开放改进意向。",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50}
                },
                "required": [],
            },
        },
    },
]

_AGENT: Any | None = None


def configure_runtime(*, agent: Any, **_: Any) -> None:
    global _AGENT
    _AGENT = agent


def _agent() -> Any:
    if _AGENT is None:
        raise RuntimeError("self_development 尚未绑定 Agent 运行时")
    return _AGENT


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def execute(tool_name: str, args: dict) -> str:
    known = {tool["function"]["name"] for tool in TOOLS}
    if tool_name not in known:
        return f"未知工具：{tool_name}，可用工具：{', '.join(sorted(known))}"
    try:
        agent = _agent()
        data = args or {}
        if tool_name == "self_development_status":
            return _dump(agent.self_development_status())
        if tool_name == "reflect_and_sediment":
            return _dump(
                agent.reflect_and_sediment(
                    note=str(data.get("note", "")),
                    deep=bool(data.get("deep", False)),
                )
            )
        if tool_name == "capability_health_snapshot":
            return _dump(agent.capability_health())
        if tool_name == "improvement_roadmap":
            return _dump(agent.improvement_roadmap(limit=int(data.get("limit", 10) or 10)))
        if tool_name == "list_self_reflections":
            return _dump(
                agent.self_reflections(limit=int(data.get("limit", 10) or 10))
            )
        if tool_name == "list_improvement_intentions":
            return _dump(
                agent.improvement_intentions(
                    status=str(data.get("status", "")),
                    limit=int(data.get("limit", 20) or 20),
                )
            )
        if tool_name == "create_improvement_intention":
            criteria = data.get("acceptance_criteria", [])
            return _dump(
                agent.create_improvement_intention(
                    title=str(data.get("title", "")),
                    rationale=str(data.get("rationale", "")),
                    priority=str(data.get("priority", "P2")),
                    acceptance_criteria=criteria if isinstance(criteria, list) else [],
                )
            )
        return _dump(
            agent.pursue_improvement_intention(
                str(data.get("intention_id", "")),
                apply_changes=bool(data.get("apply_changes", False)),
            )
        )
    except Exception as exc:
        return f"执行失败：{type(exc).__name__}: {exc}"
