"""Bounded self-optimization tools backed by ``local/self/optimizations.json``.

The capability exposes the evidence-driven "fast lane": adjust a small
whitelist of runtime parameters (memory prompt bounds and LLM temperature)
with hard range checks, per-key cooldown, bounded history, audit logging and
rollback.  It never edits code or ``config.yaml``; deeper changes still use
the app-tmp → tests → gate → promotion pipeline.  State is verifiable
software configuration, not a claim of subjective awareness.
"""

from __future__ import annotations

import json
from typing import Any

SKILL_META = {
    "name": "self_optimize",
    "description": "对白名单内的运行期参数做证据驱动的有界微调，支持审计、冷却与回滚，不修改代码与 config.yaml。",
    "version": "0.1.0",
}

CAPABILITY_META = {
    "id": "agent.self_optimization",
    "name": "证据驱动自我优化",
    "description": (
        "对运行期可调参数（记忆提示条数/字符上限、LLM 温度）做有界微调；"
        "这是安全的快车道，不涉及代码变更，所有状态可从文件与审计日志核查。"
    ),
    "version": "0.1.0",
    "domain": "agent-governance",
    "operations": [
        {
            "name": "optimize_status",
            "description": "查看白名单、当前覆盖值、历史与冷却状态",
            "risk": "read",
        },
        {
            "name": "optimize_apply",
            "description": "校验并应用一个白名单参数覆盖值",
            "risk": "change",
        },
        {
            "name": "optimize_rollback",
            "description": "把参数回滚到上一个历史值或默认值",
            "risk": "change",
        },
        {
            "name": "optimize_auto",
            "description": "基于能力健康可信证据自动微调（可能改变 active 值）",
            "risk": "change",
        },
    ],
    "composes_with": [
        "agent.self_development",
        "agent.self_reflection",
        "software.validation",
    ],
}

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "optimize_status",
            "description": (
                "查看自我优化快车道状态：可调参数白名单、当前生效覆盖、最近历史与冷却期。"
                "结果不代表主观意识。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "optimize_apply",
            "description": (
                "应用一个白名单内的运行期参数覆盖。键必须在白名单内、值必须在允许范围内，"
                "且同键一小时冷却期已过；写入有界历史并追加审计日志。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "白名单键：agent.memory_prompt_limit / agent.memory_prompt_max_chars / llm.temperature。",
                    },
                    "value": {
                        "type": "number",
                        "description": "目标值，必须在白名单允许范围内。",
                    },
                    "reason": {
                        "type": "string",
                        "description": "调整理由（会脱敏后持久化）。",
                    },
                    "evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选证据引用列表。",
                    },
                },
                "required": ["key", "value", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "optimize_rollback",
            "description": "把指定白名单参数回滚到上一个历史值；无更早历史值时恢复默认。",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "要回滚的白名单键。"}
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "optimize_auto",
            "description": (
                "触发证据驱动自动优化：读取能力健康可信结果，记忆/截断失败证据充足时"
                "收缩记忆提示块一档，连续健康则回调一档；证据不足时保持现状。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]

_AGENT: Any | None = None
_REGISTRY: Any | None = None


def configure_runtime(*, agent: Any, registry: Any = None, **_: Any) -> None:
    global _AGENT, _REGISTRY
    _AGENT = agent
    # 优先把绑定状态写到 Registry 实例的 per-instance 上下文，
    # 模块级全局仅作未传入 registry 时的兼容兜底
    if registry is not None and hasattr(registry, "bind_state"):
        _REGISTRY = registry
        registry.bind_state("self_optimize", agent=agent)


def _agent() -> Any:
    agent = None
    if _REGISTRY is not None and hasattr(_REGISTRY, "get_state"):
        agent = _REGISTRY.get_state("self_optimize").get("agent")
    if agent is None:
        agent = _AGENT
    if agent is None:
        raise RuntimeError("self_optimize 尚未绑定 Agent 运行时")
    return agent


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def execute(tool_name: str, args: dict) -> str:
    known = {tool["function"]["name"] for tool in TOOLS}
    if tool_name not in known:
        return f"未知工具：{tool_name}，可用工具：{', '.join(sorted(known))}"
    try:
        store = _agent().optimization
        data = args or {}
        if tool_name == "optimize_status":
            return _dump(store.status())
        if tool_name == "optimize_apply":
            evidence = data.get("evidence", [])
            applied, message = store.apply(
                str(data.get("key", "")),
                data.get("value"),
                str(data.get("reason", "")),
                evidence=evidence if isinstance(evidence, list) else [],
            )
            return _dump(
                {"applied": applied, "message": message, "status": store.status()}
            )
        if tool_name == "optimize_rollback":
            rolled_back, message = store.rollback(str(data.get("key", "")))
            return _dump(
                {
                    "rolled_back": rolled_back,
                    "message": message,
                    "status": store.status(),
                }
            )
        return _dump(store.auto_tune())
    except Exception as exc:
        return f"执行失败：{type(exc).__name__}: {exc}"
