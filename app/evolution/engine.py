"""Compatibility adapter for Agenelf's controlled autonomy pipeline.

The original implementation directly created Git branches and merged changes into
``main``. That path conflicts with the container security model and is therefore
retired. All self-improvement now goes through ``app-tmp`` testing, trusted gate
checks and host-side promotion.
"""

from __future__ import annotations

from typing import Any


class EvolutionError(RuntimeError):
    """Raised when the safe evolution adapter cannot start a cycle."""


class EvolutionEngine:
    """Backward-compatible facade over ``Agent.run_autonomy_cycle``."""

    def __init__(self, agent: Any):
        if not callable(getattr(agent, "run_autonomy_cycle", None)):
            raise TypeError("EvolutionEngine 需要绑定支持 run_autonomy_cycle 的 Agent")
        self.agent = agent

    def evolve(self, goal: str) -> dict:
        if not isinstance(goal, str) or not goal.strip():
            raise EvolutionError("进化目标不能为空")
        return self.agent.run_autonomy_cycle(goal=goal.strip(), apply_changes=True)

    def propose_core_change(self, goal: str, llm: Any = None) -> tuple[bool, str]:
        """Retained for callers of the legacy API, without direct Git mutation."""

        del llm
        try:
            result = self.evolve(goal)
        except Exception as exc:
            return False, str(exc)
        status = str(result.get("status", ""))
        return status == "promotion_requested", (
            "安全自主循环已提交晋升请求"
            if status == "promotion_requested"
            else f"安全自主循环未完成：{status}；{result.get('error', '')}"
        )
