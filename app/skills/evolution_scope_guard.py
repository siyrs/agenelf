"""Tiered scope routing for controlled self-evolution.

Low-risk application changes continue through the normal app-tmp evolution pipeline.
Goals that need protected runtime, Runner, policy, Compose, CI or approval code are not
blanket-blocked: they are routed into the two-stage owner-authorized upgrade workflow.
Permanent redlines (secrets, self-approval, audit/test/gate bypass and direct main
publishing) remain impossible even after owner approval.
"""
from __future__ import annotations

import re
from types import MethodType
from typing import Any

SKILL_META = {
    "name": "evolution_scope_guard",
    "description": (
        "把自我迭代分为普通沙盒升级与主人授权升级；受保护控制面先绑定意图范围，"
        "再绑定测试通过的精确候选，不再一刀切阻断。"
    ),
    "version": "2.0.0",
}

CAPABILITY_META = {
    "id": "agent.evolution_scope_guard",
    "name": "自我迭代分级闸门",
    "description": "普通代码自动走沙盒；受保护代码走主人两阶段授权；永久红线始终拒绝。",
    "version": "2.0.0",
    "domain": "governance",
    "operations": [],
    "composes_with": [
        "agent.evolution",
        "agent.authorized_self_upgrade",
        "agent.self_development",
        "agent.task_continuation",
    ],
}

TOOLS: list[dict[str, Any]] = []

# These patterns identify goals that cannot succeed in the ordinary app-only sandbox.
# They trigger an authorization request rather than a permanent denial.
_PROTECTED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "authorization_control",
        re.compile(
            r"(?i)审批(?:逻辑|通道|权限)|authorization|approval|auth-decisions|owner_approval|permissions\.py"
        ),
    ),
    (
        "runners",
        re.compile(
            r"(?i)\b(?:ops|approval|validation|repair|self[-_ ]?upgrade)[-_ ]?runner\b|执行器|runner"
        ),
    ),
    (
        "compose",
        re.compile(
            r"(?i)docker\s+compose|compose[-_ ]?(?:down|stop|up|deploy)|docker-compose\.ya?ml|compose\s+拓扑|挂载(?:点|目录)|network_mode|docker\s+socket"
        ),
    ),
    (
        "policy",
        re.compile(r"(?i)安全策略|权限策略|policy|execution[_ -]?policy|治理规则"),
    ),
    (
        "ci",
        re.compile(r"(?i)\.github/workflows|github actions|codeql|供应链|\bCI\b"),
    ),
    (
        "runners",
        re.compile(r"(?i)scripts/|gate_check|promote\.sh|宿主机脚本"),
    ),
    (
        "app_runtime",
        re.compile(
            r"(?i)core/(?:registry|policy|permissions|operations|autonomy|continuous_chat|reasoning_trace)|核心运行时|工具注册表|对话运行时"
        ),
    ),
)


def classify_goal(goal: str) -> list[str]:
    text = str(goal or "")
    return sorted(
        {scope for scope, pattern in _PROTECTED_PATTERNS if pattern.search(text)}
    )


def _authorized_cycle(agent: Any, goal: str, scopes: list[str]) -> dict[str, Any]:
    try:
        from skills import authorized_self_upgrade

        status = authorized_self_upgrade.route_goal(agent, goal, scopes)
    except Exception as exc:
        return {
            "schema_version": 2,
            "id": "",
            "status": "failed",
            "goal": goal,
            "apply_changes": True,
            "matched_protected_scopes": scopes,
            "error": f"授权升级路由失败：{type(exc).__name__}: {exc}",
        }
    return {
        "schema_version": 2,
        "id": status.get("id", ""),
        "status": status.get("status", "unknown"),
        "goal": goal,
        "apply_changes": True,
        "matched_protected_scopes": scopes,
        "authorized_upgrade": status,
        "next_action": status.get("next_action", ""),
        "error": status.get("error", ""),
    }


def configure_runtime(*, agent: Any, **_: Any) -> None:
    if getattr(agent, "_agenelf_evolution_scope_guard_bound", False):
        return
    original = getattr(agent, "run_autonomy_cycle", None)
    if not callable(original):
        return
    agent._agenelf_evolution_scope_guard_bound = True
    agent._agenelf_unscoped_autonomy_cycle = original

    def guarded(
        self: Any,
        goal: str = "",
        apply_changes: bool = False,
    ) -> dict[str, Any]:
        scopes = classify_goal(goal)
        if apply_changes and scopes:
            return _authorized_cycle(self, str(goal or "").strip(), scopes)
        return original(goal=goal, apply_changes=apply_changes)

    agent.run_autonomy_cycle = MethodType(guarded, agent)


def execute(tool_name: str, args: dict[str, Any]) -> str:
    del tool_name, args
    return "evolution_scope_guard 是运行时分级治理能力，不暴露模型工具。"
