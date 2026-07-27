"""Configure bounded multi-segment continuation for long tool-driven tasks.

分段续跑主回路已并入 ``core.agent.Agent.chat``（单一主回路）。本技能不再
替换 ``agent.chat``，只是配置壳：把预算参数写入 agent 配置属性与 registry
per-instance 状态；重复 ``configure_runtime`` 幂等。
"""
from __future__ import annotations

from typing import Any

from core.continuous_chat import configured_no_progress_limit, configured_segments

SKILL_META = {
    "name": "tool_budget_continuation",
    "description": (
        "单段工具轮次耗尽后保持同一会话自动续跑，实时刷新技能清单；"
        "总预算仍耗尽时保存可恢复检查点，而不是返回固定失败文本。"
    ),
    "version": "1.0.0",
}

CAPABILITY_META = {
    "id": "agent.tool_budget_continuation",
    "name": "工具任务自动续跑",
    "description": (
        "把一次复杂任务拆成多个有界工具段，在同一模型上下文中连续执行，"
        "并与重启安全的任务检查点组合。"
    ),
    "version": "1.0.0",
    "domain": "orchestration",
    "operations": [],
    "composes_with": [
        "agent.task_continuation",
        "agent.reasoning_trace",
        "agent.self_development",
        "agent.workflow",
    ],
}

TOOLS: list[dict[str, Any]] = []


def configure_runtime(
    *,
    agent: Any,
    registry: Any | None = None,
    config: dict[str, Any] | None = None,
    **_: Any,
) -> None:
    """把分段预算写入 agent 配置属性与 registry per-instance 状态（幂等）。"""

    full = config if isinstance(config, dict) else getattr(agent, "config", {})
    segments = configured_segments(full)
    repeat_limit = configured_no_progress_limit(full)
    rounds = max(1, int(getattr(agent, "max_tool_rounds", 8)))
    agent.max_tool_segments = segments
    agent.no_progress_repeat_limit = repeat_limit
    agent.max_total_tool_rounds = rounds * segments
    if registry is None:
        registry = getattr(agent, "registry", None)
    bind_state = getattr(registry, "bind_state", None)
    if callable(bind_state):
        bind_state(
            "tool_budget_continuation",
            max_tool_segments=segments,
            no_progress_repeat_limit=repeat_limit,
            max_total_tool_rounds=rounds * segments,
        )


def execute(tool_name: str, args: dict[str, Any]) -> str:
    del tool_name, args
    return "tool_budget_continuation 是运行时编排能力，不暴露模型可直接调用的工具。"
