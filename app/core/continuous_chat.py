"""Bounded multi-segment chat execution for long tool-driven tasks.

The legacy :class:`core.agent.Agent` stops after one ``max_tool_rounds`` block and
returns a fixed failure sentence.  This runtime keeps the same conversation and tool
messages alive across several bounded segments, refreshes the system prompt and tool
catalog after every tool batch, and creates a restart-safe checkpoint only when the
whole bounded budget is genuinely exhausted.
"""
from __future__ import annotations

import importlib
import json
import os
from types import MethodType
from typing import Any

from core.privacy import redact_sensitive_text

LEGACY_EXHAUSTION_TEXT = "（已达到最大工具调用轮数，任务尚未完成）"
_DEFAULT_MAX_SEGMENTS = 4
_MAX_SEGMENTS_LIMIT = 16
_MAX_ROUNDS_PER_SEGMENT = 128


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def configured_segments(config: dict[str, Any] | None) -> int:
    """Resolve the bounded continuation segment count from env/config."""

    config = config if isinstance(config, dict) else {}
    agent_cfg = config.get("agent", {})
    if not isinstance(agent_cfg, dict):
        agent_cfg = {}
    raw = os.environ.get(
        "AGENELF_MAX_TOOL_SEGMENTS",
        str(agent_cfg.get("max_tool_segments", _DEFAULT_MAX_SEGMENTS)),
    )
    return _bounded_int(raw, _DEFAULT_MAX_SEGMENTS, 1, _MAX_SEGMENTS_LIMIT)


def _refresh_turn_runtime(
    agent: Any,
    messages: list[dict[str, Any]],
    continuation_note: str = "",
) -> list[dict[str, Any]] | None:
    """Refresh prompt and tools without losing in-flight assistant/tool messages."""

    agent._refresh_system_prompt()
    prompt = str(agent.system_prompt)
    if continuation_note:
        prompt += "\n\n" + continuation_note
    if messages and messages[0].get("role") == "system":
        messages[0] = {"role": "system", "content": prompt}
    else:
        messages.insert(0, {"role": "system", "content": prompt})
    return agent.registry.all_tool_schemas() or None


def _dispatch(agent: Any, call: dict[str, Any], subject: str) -> str:
    name = str(call.get("name", ""))
    arguments = call.get("arguments", {})
    if not isinstance(arguments, dict):
        arguments = {}
    try:
        return str(agent.registry.dispatch(name, arguments, subject=subject))
    except TypeError as exc:
        # Compatibility with tests/extensions still exposing the historical two-arg
        # dispatch signature.  Unrelated TypeError exceptions must still surface.
        if "unexpected keyword argument 'subject'" not in str(exc):
            raise
        return str(agent.registry.dispatch(name, arguments))


def _assistant_tool_message(
    response: dict[str, Any], tool_calls: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": response.get("content"),
        "tool_calls": [
            {
                "id": str(call.get("id", "")),
                "type": "function",
                "function": {
                    "name": str(call.get("name", "")),
                    "arguments": json.dumps(
                        call.get("arguments", {})
                        if isinstance(call.get("arguments", {}), dict)
                        else {},
                        ensure_ascii=False,
                    ),
                },
            }
            for call in tool_calls
        ],
    }


def _checkpoint_exhausted_task(
    agent: Any,
    user_input: str,
    *,
    completed_rounds: int,
    segment_rounds: int,
    segments: int,
) -> str:
    """Persist a safe resume checkpoint instead of returning the legacy dead end."""

    try:
        continuation = importlib.import_module("skills.task_continuation")
        value = continuation.checkpoint(
            task_summary=user_input,
            resume_prompt=(
                "继续完成原始任务。上一轮已经执行了多个工具步骤但总预算耗尽。"
                "先读取现有任务、改进意向、运维结果、测试结果和晋升状态，复用已有证据，"
                "不要重复无进展调用；使用当前最新技能和工具清单继续。"
                "只有真实完成、等待具体审批或存在无法自动消除的外部阻塞时才结束。"
            ),
            reason="automatic_tool_budget_exhaustion",
            expires_minutes=1440,
            max_attempts=2,
        )
        continuation_id = str(value.get("id", ""))
        return (
            "当前任务在一次请求内已自动续跑 "
            f"{segments} 个工具段、共 {completed_rounds} 个模型轮次，仍未真实完成。\n\n"
            f"已保存可恢复检查点：{continuation_id or '（ID 未返回）'}\n"
            "下次进入 CLI 时会从检查点自动续跑；不会把预算耗尽伪装成任务完成。\n"
            "任何新的远程变更仍需按原策略单独审批。"
        )
    except Exception as exc:  # checkpoint failure must not hide the original outcome
        safe_error = redact_sensitive_text(f"{type(exc).__name__}: {exc}")[:1000]
        return (
            "当前任务已自动续跑至总工具预算上限，仍未真实完成。\n\n"
            f"已执行：{segments} 个工具段 / {completed_rounds} 个模型轮次\n"
            f"自动保存续跑检查点失败：{safe_error}\n"
            "请保留当前会话后继续；系统没有把未完成状态伪装为成功。"
        )


def continuous_chat(agent: Any, user_input: str, *, subject: str = "agent") -> str:
    """Run one user turn across multiple bounded tool segments."""

    try:
        agent.llm.temperature = float(
            agent.optimization.get_effective(
                "llm.temperature",
                agent.config.get("llm", {}).get("temperature", 0.6),
            )
        )
    except (AttributeError, TypeError, ValueError):
        pass

    agent._refresh_system_prompt()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": agent.system_prompt},
        *agent.history,
        {"role": "user", "content": user_input},
    ]
    tools = agent.registry.all_tool_schemas() or None
    final_text = ""
    tool_used = False
    completed_rounds = 0
    continuation_note = ""

    segment_rounds = _bounded_int(
        getattr(agent, "max_tool_rounds", 8),
        8,
        1,
        _MAX_ROUNDS_PER_SEGMENT,
    )
    segments = configured_segments(getattr(agent, "config", {}))
    total_round_budget = segment_rounds * segments
    agent.max_tool_segments = segments
    agent.max_total_tool_rounds = total_round_budget

    for round_index in range(total_round_budget):
        completed_rounds = round_index + 1
        response = agent.llm.chat(messages, tools=tools)
        if not isinstance(response, dict):
            raise TypeError("LLM chat 必须返回 dict")
        raw_calls = response.get("tool_calls") or []
        tool_calls = [call for call in raw_calls if isinstance(call, dict)]
        if not tool_calls:
            final_text = str(response.get("content") or "")
            break

        messages.append(_assistant_tool_message(response, tool_calls))
        for call in tool_calls:
            tool_used = True
            call_id = str(call.get("id", ""))
            result = _dispatch(agent, call, subject)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": str(call.get("name", "")),
                    "content": result,
                }
            )

        # Skills can be promoted or reloaded during a tool batch.  Rebuild both the
        # prompt and tool catalog immediately so the next model round can use them.
        if completed_rounds % segment_rounds == 0 and completed_rounds < total_round_budget:
            current_segment = completed_rounds // segment_rounds
            continuation_note = (
                "【运行时自动续跑】\n"
                f"已完成第 {current_segment}/{segments} 个有界工具段，当前仍是同一用户任务。"
                "继续使用已有工具结果和当前最新技能完成最初目标；不要重复无进展调用。"
                "只有任务真实完成、等待绑定具体参数的审批，或存在外部阻塞时才结束。"
            )
        tools = _refresh_turn_runtime(agent, messages, continuation_note)
    else:
        final_text = _checkpoint_exhausted_task(
            agent,
            user_input,
            completed_rounds=completed_rounds,
            segment_rounds=segment_rounds,
            segments=segments,
        )

    if not final_text:
        final_text = "（未获得有效回复）"

    agent._append_history(user_input, final_text)
    summary = f"用户：{user_input} | 助手：{final_text[:200]}"
    if tool_used:
        summary = (
            f"[含工具调用 rounds={completed_rounds}/{total_round_budget}] " + summary
        )
    agent.memory.add("episode", summary)
    agent._maybe_auto_reflect()
    agent._refresh_system_prompt()
    return final_text


def install_continuous_chat(agent: Any, config: dict[str, Any] | None = None) -> None:
    """Install the bounded chat runtime exactly once on an Agent instance."""

    if getattr(agent, "_agenelf_continuous_chat_bound", False):
        return
    agent._agenelf_continuous_chat_bound = True
    agent.max_tool_segments = configured_segments(config or getattr(agent, "config", {}))
    agent.max_total_tool_rounds = max(1, int(getattr(agent, "max_tool_rounds", 8))) * agent.max_tool_segments

    def bound_chat(self: Any, user_input: str, *, subject: str = "agent") -> str:
        return continuous_chat(self, user_input, subject=subject)

    agent.chat = MethodType(bound_chat, agent)
