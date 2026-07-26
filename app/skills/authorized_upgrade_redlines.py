"""Runtime binding for diff-aware owner-authorized upgrade redlines."""
from __future__ import annotations

from typing import Any

from core import authorized_upgrade, upgrade_redlines

SKILL_META = {
    "name": "authorized_upgrade_redlines",
    "description": "仅扫描候选新增代码并保持可信升级根约束，避免合法审批代码维护被旧内容误判。",
    "version": "1.0.0",
}

CAPABILITY_META = {
    "id": "agent.authorized_upgrade_redlines",
    "name": "授权升级永久红线",
    "description": "在 Agent 候选阶段安装差异感知的永久红线检查。",
    "version": "1.0.0",
    "domain": "governance",
    "operations": [],
    "composes_with": ["agent.authorized_self_upgrade"],
}

TOOLS: list[dict[str, Any]] = []


def configure_runtime(*, agent: Any, **_: Any) -> None:
    upgrade_redlines.install(authorized_upgrade)
    try:
        from core import autonomy

        current = set(getattr(autonomy, "_PROTECTED_PATHS", frozenset()))
        autonomy._PROTECTED_PATHS = frozenset(
            current
            | {
                "core/upgrade_redlines.py",
                "skills/authorized_upgrade_redlines.py",
            }
        )
    except Exception:
        # The host gate and governance validator independently enforce the same rule.
        pass
    del agent


def execute(tool_name: str, args: dict[str, Any]) -> str:
    del tool_name, args
    return "authorized_upgrade_redlines 是运行时治理能力，不暴露模型工具。"
