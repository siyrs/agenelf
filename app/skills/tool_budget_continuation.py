"""Install bounded multi-segment continuation for long tool-driven tasks."""
from __future__ import annotations

from typing import Any

from core.continuous_chat import install_continuous_chat

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
    config: dict[str, Any] | None = None,
    **_: Any,
) -> None:
    install_continuous_chat(agent, config or getattr(agent, "config", {}))


def execute(tool_name: str, args: dict[str, Any]) -> str:
    del tool_name, args
    return "tool_budget_continuation 是运行时编排能力，不暴露模型可直接调用的工具。"
