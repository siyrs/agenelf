"""Runtime continuity layer for skill upgrades and bounded tool loops.

The core chat loop remains unchanged. This skill adds three deterministic behaviors:
1. every rebuilt system prompt states that a skill upgrade is an intermediate step;
2. every model call receives the latest system prompt and registry tool schemas, so a
   skill registered during the current turn is usable on the very next tool round;
3. the legacy max-tool-round sentinel automatically starts another bounded segment,
   using fresh tool schemas and the preserved conversation history.
"""

from __future__ import annotations

import os
from types import MethodType
from typing import Any

SKILL_META = {
    "name": "task_continuation",
    "description": "技能热加载后自动续办原任务，并在总预算耗尽时输出可恢复检查点。",
    "version": "1.1.0",
}

CAPABILITY_META = {
    "id": "agent.task_continuation",
    "name": "任务连续性",
    "description": "跨技能重载与多段工具预算持续执行同一用户目标。",
    "version": "1.1.0",
    "domain": "autonomy",
    "composes_with": ["agent.self_development", "server.docker", "agent.workflow"],
    "operations": [],
}

TOOLS: list[dict[str, Any]] = []

_MAX_ROUND_SENTINEL = "（已达到最大工具调用轮数，任务尚未完成）"
_PROMPT_MARKER = "[任务连续性运行时约束]"


def _segment_budget(config: dict[str, Any] | None) -> int:
    raw = os.environ.get("AGENELF_CONTINUATION_SEGMENTS", "").strip()
    if not raw and isinstance(config, dict):
        agent_cfg = config.get("agent", {})
        if isinstance(agent_cfg, dict):
            raw = str(agent_cfg.get("continuation_segments", "")).strip()
    try:
        value = int(raw or "3")
    except ValueError:
        value = 3
    return max(2, min(value, 6))


def _bind_fresh_model_context(agent: Any, registry: Any | None) -> None:
    """Make each model call observe skills registered during the current turn."""

    llm = getattr(agent, "llm", None)
    current_chat = getattr(llm, "chat", None)
    if (
        llm is None
        or registry is None
        or not callable(current_chat)
        or getattr(llm, "_task_continuation_fresh_context_bound", False)
    ):
        return

    original_llm_chat = current_chat

    def chat_with_fresh_context(
        self: Any,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        # skill_forge and other governed registration paths refresh
        # agent.system_prompt. Replace only the system message for the current model
        # call; keep the accumulated assistant/tool messages untouched.
        if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
            messages[0] = {**messages[0], "content": str(agent.system_prompt)}
        fresh_tools = registry.all_tool_schemas() or None
        return original_llm_chat(messages, tools=fresh_tools)

    llm.chat = MethodType(chat_with_fresh_context, llm)
    llm._task_continuation_fresh_context_bound = True


def configure_runtime(
    *,
    agent: Any,
    registry: Any | None = None,
    config: dict[str, Any] | None = None,
    **_: Any,
) -> None:
    """Bind continuity once to an Agent instance without replacing core source."""

    if getattr(agent, "_task_continuation_bound", False):
        return
    agent._task_continuation_bound = True
    segments = _segment_budget(config)

    original_refresh = agent._refresh_system_prompt

    def refresh_with_continuity(self: Any) -> None:
        original_refresh()
        if _PROMPT_MARKER not in self.system_prompt:
            self.system_prompt += (
                "\n\n"
                f"{_PROMPT_MARKER}\n"
                "- 为完成当前任务而新增、升级或重载技能时，技能变更只是中间步骤；"
                "必须使用刷新后的工具继续最初目标。\n"
                "- 不得因为单段工具轮次耗尽而向主人返回笼统失败；运行时会开启下一段，"
                "应从历史、记忆和已有证据继续，不重复询问已提供的信息。\n"
                "- 只有任务完成、等待绑定具体参数的外部审批，或存在无法自动消除的外部阻塞时才结束。"
            )

    agent._refresh_system_prompt = MethodType(refresh_with_continuity, agent)
    _bind_fresh_model_context(agent, registry)

    original_chat = agent.chat

    def chat_with_continuation(
        self: Any, user_input: str, *, subject: str = "agent"
    ) -> str:
        original_goal = str(user_input)
        reply = original_chat(original_goal, subject=subject)
        used_segments = 1

        while _MAX_ROUND_SENTINEL in reply and used_segments < segments:
            used_segments += 1
            continuation = (
                "继续完成上一段尚未完成的同一任务，不要停在技能升级、能力缺口说明或"
                "最大轮次提示上。请从对话历史、长期记忆、已有工具结果和当前最新技能"
                "直接恢复执行。\n\n"
                f"原始用户目标：{original_goal}\n"
                f"这是自动续办的第 {used_segments}/{segments} 段。"
            )
            reply = original_chat(continuation, subject=subject)

        if _MAX_ROUND_SENTINEL not in reply:
            return reply

        checkpoint = (
            "可恢复检查点\n"
            f"- 原始目标：{original_goal}\n"
            f"- 已自动续办：{used_segments} 个有界工具段\n"
            "- 当前状态：仍达到总工具预算上限，未伪装为已完成\n"
            "- 下一动作：下一轮直接读取最近历史和记忆，从未完成的工具步骤继续；"
            "不要重复询问已提供的服务器、授权或目标信息。"
        )
        try:
            self.memory.add("episode", checkpoint)
        except Exception:
            pass
        return checkpoint

    agent.chat = MethodType(chat_with_continuation, agent)
    agent._refresh_system_prompt()


def execute(tool_name: str, args: dict[str, Any]) -> str:
    return "task_continuation 是运行时能力，不暴露可由模型直接调用的工具。"
