"""Self-model and controlled autonomy tools.

This skill exposes observable runtime facts and a sandboxed improvement cycle. It
must never describe Agenelf as conscious or bypass the evolution gate.
"""

from __future__ import annotations

import json
from typing import Any

from core.autonomy import AutonomyEngine

SKILL_META = {
    "name": "self_reflection",
    "description": "建立可审计的自我模型，检查能力与安全状态，并触发受控的沙盒自主改进循环。",
    "version": "0.1.0",
}

CAPABILITY_META = {
    "id": "agent.self_reflection",
    "name": "自我模型与自主反思",
    "description": "观察自身能力、限制和运行状态，生成改进计划，并可把小型带测试补丁送入安全晋升管道。",
    "version": "0.1.0",
    "domain": "agent-governance",
    "operations": [
        {"name": "self_snapshot", "description": "返回当前可观测自我模型与安全不变量", "risk": "read"},
        {"name": "self_assess", "description": "检查能力缺口、加载错误与迭代状态", "risk": "read"},
        {"name": "autonomy_cycle", "description": "生成计划，或在 app-tmp 内执行一次带测试的自主改进", "risk": "change"},
        {"name": "autonomy_status", "description": "查询最近的自主循环记录", "risk": "read"},
    ],
    "composes_with": [
        "software.validation",
        "server.operations",
        "code.repair",
        "software.release",
    ],
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "self_snapshot",
            "description": "查看 Agenelf 当前加载的技能、能力域、运行队列、自我迭代状态与不可违反的安全不变量。该结果是软件运行状态，不代表主观意识。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "self_assess",
            "description": "基于当前可观测状态做一次自我检查，输出 P0/P1/P2 缺口和推荐改进目标，不修改代码。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "autonomy_cycle",
            "description": "执行一次受控自主循环。apply_changes=false 只生成计划；true 时只在 app-tmp 生成最多四个 Python 文件的补丁，强制包含测试，测试通过后仅申请晋升。",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "可选改进目标；留空时根据自我检查自动选择最高优先级目标。",
                    },
                    "apply_changes": {
                        "type": "boolean",
                        "description": "是否生成并测试沙盒补丁；默认 false。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "autonomy_status",
            "description": "查看指定自主循环或最近十次循环的状态、证据和失败原因。",
            "parameters": {
                "type": "object",
                "properties": {
                    "cycle_id": {"type": "string", "description": "可选 auto- 开头的循环 ID。"},
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


def _engine() -> AutonomyEngine:
    if _AGENT is None:
        raise RuntimeError("self_reflection 尚未绑定 Agent 运行时")
    return AutonomyEngine(_AGENT)


def _dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def execute(tool_name: str, args: dict) -> str:
    known = {tool["function"]["name"] for tool in TOOLS}
    if tool_name not in known:
        return f"未知工具：{tool_name}，可用工具：{', '.join(sorted(known))}"
    try:
        engine = _engine()
        if tool_name == "self_snapshot":
            return _dump(engine.snapshot())
        if tool_name == "self_assess":
            return _dump(engine.assess())
        if tool_name == "autonomy_cycle":
            return _dump(
                engine.run_cycle(
                    goal=str((args or {}).get("goal", "")),
                    apply_changes=bool((args or {}).get("apply_changes", False)),
                )
            )
        cycle_id = str((args or {}).get("cycle_id", "")).strip()
        return _dump(engine.get_cycle(cycle_id) if cycle_id else engine.latest_cycles())
    except Exception as exc:
        return f"执行失败：{type(exc).__name__}: {exc}"
