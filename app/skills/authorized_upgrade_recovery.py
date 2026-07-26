"""Runtime binding for bounded authorized-upgrade candidate recovery."""
from __future__ import annotations

from typing import Any

from core import authorized_upgrade, authorized_upgrade_recovery

SKILL_META = {
    "name": "authorized_upgrade_recovery",
    "description": (
        "在同一主人授权范围内有限重试候选生成，把上一轮测试证据带入下一轮，"
        "并在第二次批准前展示精确文件摘要。"
    ),
    "version": "1.0.0",
}

CAPABILITY_META = {
    "id": "agent.authorized_upgrade_recovery",
    "name": "授权升级恢复",
    "description": "处理模型中断、候选测试失败、候选拒绝和授权过期，不扩大原授权范围。",
    "version": "1.0.0",
    "domain": "reliability",
    "operations": [],
    "composes_with": [
        "agent.authorized_self_upgrade",
        "agent.transport_resilience",
        "agent.task_continuation",
    ],
}

TOOLS: list[dict[str, Any]] = []


def configure_runtime(*, agent: Any, **_: Any) -> None:
    del agent
    authorized_upgrade_recovery.install(authorized_upgrade)
    try:
        from core import autonomy

        current = set(getattr(autonomy, "_PROTECTED_PATHS", frozenset()))
        autonomy._PROTECTED_PATHS = frozenset(
            current
            | {
                "core/authorized_upgrade_recovery.py",
                "skills/authorized_upgrade_recovery.py",
            }
        )
    except Exception:
        pass


def execute(tool_name: str, args: dict[str, Any]) -> str:
    del tool_name, args
    return "authorized_upgrade_recovery 是运行时可靠性能力，不暴露模型工具。"
