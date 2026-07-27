"""Backward-compatible shim for the former monkeypatched chat loop.

分段工具预算 / 检查点续跑逻辑已经并入 ``core.agent.Agent.chat`` 单一主回路，
本模块只保留兼容导出，**不再**通过 ``MethodType`` 替换 ``agent.chat``：

- ``LEGACY_EXHAUSTION_TEXT`` / ``configured_segments`` /
  ``configured_no_progress_limit``：从 ``core.agent`` 转出的常量与配置助手；
- ``continuous_chat(agent, ...)``：deprecated，直接委托 ``agent.chat``；
- ``install_continuous_chat(agent, config)``：deprecated，仅写入分段预算
  配置（幂等），不再替换 ``agent.chat``。

新代码请直接使用 ``Agent.chat`` 与 ``Agent.add_llm_wrapper`` 钩子管线。
"""
from __future__ import annotations

from typing import Any

from core.agent import (
    LEGACY_EXHAUSTION_TEXT,
    configured_no_progress_limit,
    configured_segments,
)

__all__ = [
    "LEGACY_EXHAUSTION_TEXT",
    "configured_no_progress_limit",
    "configured_segments",
    "continuous_chat",
    "install_continuous_chat",
]


def continuous_chat(agent: Any, user_input: str, *, subject: str = "agent") -> str:
    """Deprecated: 委托合并后的 ``Agent.chat`` 单一主回路。"""

    return agent.chat(user_input, subject=subject)


def install_continuous_chat(agent: Any, config: dict[str, Any] | None = None) -> None:
    """Deprecated: 只写入分段预算配置，不再替换 ``agent.chat``（幂等）。"""

    full = config if isinstance(config, dict) else getattr(agent, "config", {})
    segments = configured_segments(full)
    agent.max_tool_segments = segments
    agent.no_progress_repeat_limit = configured_no_progress_limit(full)
    agent.max_total_tool_rounds = (
        max(1, int(getattr(agent, "max_tool_rounds", 8))) * segments
    )
