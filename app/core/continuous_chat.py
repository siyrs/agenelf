"""Bounded multi-segment chat execution for long tool-driven tasks.

A task can cross several tool segments while retaining one conversation.  The runtime
also detects repeated no-progress tool batches and converts unrecovered model transport
failures into restart-safe checkpoints, so neither an infinite loop nor a broken HTTP
stream terminates the interactive CLI.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
from types import MethodType
from typing import Any

from core.privacy import redact_sensitive_text

LEGACY_EXHAUSTION_TEXT = "（已达到最大工具调用轮数，任务尚未完成）"
_DEFAULT_MAX_SEGMENTS = 4
_MAX_SEGMENTS_LIMIT = 16
_MAX_ROUNDS_PER_SEGMENT = 128
_DEFAULT_NO_PROGRESS_LIMIT = 3
_DYNAMIC_ID_RE = re.compile(
    r"\b(?:op-[0-9a-f]{16}|auth-[0-9a-f]{12}|resume-[A-Za-z0-9._-]+|"
    r"auto-[A-Za-z0-9._-]+|evo-[A-Za-z0-9._-]+|call-[A-Za-z0-9._-]+)\b"
)
_TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def configured_segments(config: dict[str, Any] | None) -> int:
    config = config if isinstance(config, dict) else {}
    agent_cfg = config.get("agent", {})
    if not isinstance(agent_cfg, dict):
        agent_cfg = {}
    raw = os.environ.get(
        "AGENELF_MAX_TOOL_SEGMENTS",
        str(agent_cfg.get("max_tool_segments", _DEFAULT_MAX_SEGMENTS)),
    )
    return _bounded_int(raw, _DEFAULT_MAX_SEGMENTS, 1, _MAX_SEGMENTS_LIMIT)


def configured_no_progress_limit(config: dict[str, Any] | None) -> int:
    config = config if isinstance(config, dict) else {}
    agent_cfg = config.get("agent", {})
    if not isinstance(agent_cfg, dict):
        agent_cfg = {}
    raw = os.environ.get(
        "AGENELF_NO_PROGRESS_REPEAT_LIMIT",
        str(agent_cfg.get("no_progress_repeat_limit", _DEFAULT_NO_PROGRESS_LIMIT)),
    )
    return _bounded_int(raw, _DEFAULT_NO_PROGRESS_LIMIT, 2, 10)


def _refresh_turn_runtime(
    agent: Any,
    messages: list[dict[str, Any]],
    continuation_note: str = "",
) -> list[dict[str, Any]] | None:
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
        if "unexpected keyword argument 'subject'" not in str(exc):
            raise
        return str(agent.registry.dispatch(name, arguments))


def _assistant_tool_message(
    response: dict[str, Any], tool_calls: list[dict[str, Any]]
) -> dict[str, Any]:
    message: dict[str, Any] = {
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
    if response.get("reasoning_content"):
        message["reasoning_content"] = response.get("reasoning_content")
    return message


def _normalize_progress_text(value: object) -> str:
    text = redact_sensitive_text(str(value or ""))
    text = _DYNAMIC_ID_RE.sub("<dynamic-id>", text)
    text = _TIMESTAMP_RE.sub("<timestamp>", text)
    return " ".join(text.split())[:4000]


def _batch_signature(records: list[dict[str, Any]]) -> str:
    normalized = []
    for record in records:
        normalized.append(
            {
                "name": str(record.get("name", "")),
                "arguments": record.get("arguments", {})
                if isinstance(record.get("arguments"), dict)
                else {},
                "result": _normalize_progress_text(record.get("result", "")),
            }
        )
    encoded = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _close_reasoning(agent: Any) -> None:
    close = getattr(getattr(agent, "llm", None), "close_reasoning_display", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _checkpoint_interrupted_task(
    user_input: str,
    *,
    reason: str,
    headline: str,
    detail: str,
    completed_rounds: int,
    segments: int,
) -> str:
    safe_detail = redact_sensitive_text(detail)[:3000]
    try:
        continuation = importlib.import_module("skills.task_continuation")
        value = continuation.checkpoint(
            task_summary=user_input,
            resume_prompt=(
                "继续完成原始任务。先读取已有工具结果、测试证据、运维请求和晋升状态，"
                "不要重复上一轮无进展调用。若上次是模型传输失败，重新获取当前状态后再继续；"
                "若目标涉及宿主机控制面，则转为人类主导仓库变更。"
                "只有真实完成、等待具体审批或存在明确外部阻塞时才结束。"
            ),
            reason=reason,
            expires_minutes=1440,
            max_attempts=3,
        )
        continuation_id = str(value.get("id", ""))
        return (
            f"{headline}\n\n"
            f"已执行模型轮次：{completed_rounds}；有界工具段：{segments}\n"
            f"最后证据：{safe_detail or '（无）'}\n\n"
            f"已保存可恢复检查点：{continuation_id or '（ID 未返回）'}\n"
            "CLI 保持可用；下次进入时会从检查点继续，不会伪装成任务完成。"
        )
    except Exception as exc:
        safe_error = redact_sensitive_text(f"{type(exc).__name__}: {exc}")[:1000]
        return (
            f"{headline}\n\n"
            f"已执行模型轮次：{completed_rounds}；有界工具段：{segments}\n"
            f"最后证据：{safe_detail or '（无）'}\n"
            f"保存续跑检查点失败：{safe_error}\n"
            "CLI 保持可用，请修复该控制面问题后继续当前任务。"
        )


def _checkpoint_exhausted_task(
    user_input: str,
    *,
    completed_rounds: int,
    segment_rounds: int,
    segments: int,
) -> str:
    del segment_rounds
    return _checkpoint_interrupted_task(
        user_input,
        reason="automatic_tool_budget_exhaustion",
        headline=(
            f"当前任务已自动续跑 {segments} 个工具段、共 {completed_rounds} 个模型轮次，"
            "仍未真实完成。"
        ),
        detail="总工具预算耗尽",
        completed_rounds=completed_rounds,
        segments=segments,
    )


def continuous_chat(agent: Any, user_input: str, *, subject: str = "agent") -> str:
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
    last_batch_signature = ""
    repeated_batches = 0
    last_batch_summary = ""

    segment_rounds = _bounded_int(
        getattr(agent, "max_tool_rounds", 8), 8, 1, _MAX_ROUNDS_PER_SEGMENT
    )
    segments = configured_segments(getattr(agent, "config", {}))
    repeat_limit = configured_no_progress_limit(getattr(agent, "config", {}))
    total_round_budget = segment_rounds * segments
    agent.max_tool_segments = segments
    agent.max_total_tool_rounds = total_round_budget
    agent.no_progress_repeat_limit = repeat_limit

    for round_index in range(total_round_budget):
        completed_rounds = round_index + 1
        try:
            response = agent.llm.chat(messages, tools=tools)
        except Exception as exc:
            _close_reasoning(agent)
            final_text = _checkpoint_interrupted_task(
                user_input,
                reason="llm_request_failure",
                headline="模型请求在有界重试后仍失败，当前 CLI 没有退出。",
                detail=f"{type(exc).__name__}: {exc}",
                completed_rounds=completed_rounds,
                segments=segments,
            )
            break
        if not isinstance(response, dict):
            final_text = _checkpoint_interrupted_task(
                user_input,
                reason="invalid_model_response",
                headline="模型返回了无效响应，当前任务已安全暂停。",
                detail=f"response_type={type(response).__name__}",
                completed_rounds=completed_rounds,
                segments=segments,
            )
            break

        raw_calls = response.get("tool_calls") or []
        tool_calls = [call for call in raw_calls if isinstance(call, dict)]
        if not tool_calls:
            final_text = str(response.get("content") or "")
            break

        messages.append(_assistant_tool_message(response, tool_calls))
        batch_records: list[dict[str, Any]] = []
        for call in tool_calls:
            tool_used = True
            call_id = str(call.get("id", ""))
            result = _dispatch(agent, call, subject)
            arguments = call.get("arguments", {})
            batch_records.append(
                {
                    "name": str(call.get("name", "")),
                    "arguments": arguments if isinstance(arguments, dict) else {},
                    "result": result,
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": str(call.get("name", "")),
                    "content": result,
                }
            )

        signature = _batch_signature(batch_records)
        if signature == last_batch_signature:
            repeated_batches += 1
        else:
            last_batch_signature = signature
            repeated_batches = 1
        last_batch_summary = "; ".join(
            f"{record['name']}: {_normalize_progress_text(record['result'])[:500]}"
            for record in batch_records
        )
        if repeated_batches >= repeat_limit:
            final_text = _checkpoint_interrupted_task(
                user_input,
                reason="automatic_no_progress_loop",
                headline=(
                    f"检测到同一工具结果连续重复 {repeated_batches} 次，已停止无进展循环。"
                ),
                detail=last_batch_summary,
                completed_rounds=completed_rounds,
                segments=segments,
            )
            break

        if completed_rounds % segment_rounds == 0 and completed_rounds < total_round_budget:
            current_segment = completed_rounds // segment_rounds
            continuation_note = (
                "【运行时自动续跑】\n"
                f"已完成第 {current_segment}/{segments} 个有界工具段，当前仍是同一用户任务。"
                "继续使用已有工具结果和当前最新技能完成最初目标；不要重复无进展调用。"
                "若连续获得相同失败，停止并报告确定性根因，不得修改测试或策略绕过。"
            )
        tools = _refresh_turn_runtime(agent, messages, continuation_note)
    else:
        final_text = _checkpoint_exhausted_task(
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
        summary = f"[含工具调用 rounds={completed_rounds}/{total_round_budget}] " + summary
    agent.memory.add("episode", summary)
    agent._maybe_auto_reflect()
    agent._refresh_system_prompt()
    return final_text


def install_continuous_chat(agent: Any, config: dict[str, Any] | None = None) -> None:
    if getattr(agent, "_agenelf_continuous_chat_bound", False):
        return
    agent._agenelf_continuous_chat_bound = True
    agent.max_tool_segments = configured_segments(config or getattr(agent, "config", {}))
    agent.no_progress_repeat_limit = configured_no_progress_limit(
        config or getattr(agent, "config", {})
    )
    agent.max_total_tool_rounds = (
        max(1, int(getattr(agent, "max_tool_rounds", 8))) * agent.max_tool_segments
    )

    def bound_chat(self: Any, user_input: str, *, subject: str = "agent") -> str:
        return continuous_chat(self, user_input, subject=subject)

    agent.chat = MethodType(bound_chat, agent)
