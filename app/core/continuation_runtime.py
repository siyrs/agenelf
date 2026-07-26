"""Bounded continuation and process-restart recovery for Agenelf chat."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from types import MethodType
from typing import Any

from . import continuation_registry as registry_runtime
from . import continuation_scope as scope
from . import continuation_store as store

MAX_ROUND_SENTINEL = "（已达到最大工具调用轮数，任务尚未完成）"
PROMPT_MARKER = "[任务连续性运行时约束]"


def continuation_prompt(
    checkpoint: dict[str, Any], segment: int, total: int
) -> str:
    context = checkpoint.get("recent_user_context", [])
    context_text = (
        "\n".join(f"- {item}" for item in context)
        if isinstance(context, list)
        else ""
    )
    scope_text = json.dumps(
        checkpoint.get("scope", {}), ensure_ascii=False, sort_keys=True
    )
    authorization = checkpoint.get("authorization", {})
    authorization_text = (
        "主人已明确授权在当前服务器身份和 Docker 能力范围内升级技能；"
        "该授权不替代任何外部变更的 exact approval。"
        if isinstance(authorization, dict) and authorization.get("granted")
        else "没有额外授权；遵循普通审批规则。"
    )
    return (
        "继续完成同一项尚未完成的任务。不要停在技能升级、能力缺口说明或最大轮次提示；"
        "直接使用当前最新 generation 的工具恢复执行，并复用已有运维请求和证据。\n\n"
        f"检查点：{checkpoint.get('id')}\n"
        f"原始目标：{checkpoint.get('original_goal')}\n"
        f"此前用户上下文：\n{context_text or '- （无）'}\n"
        f"受限任务作用域：{scope_text}\n"
        f"授权状态：{authorization_text}\n"
        f"这是自动续办的第 {segment}/{total} 段。"
    )


def run_segments(
    *,
    agent: Any,
    original_chat: Any,
    checkpoint: dict[str, Any],
    subject: str,
    segments: int,
    root: Path,
    first_prompt: str,
) -> str:
    reply = MAX_ROUND_SENTINEL
    for index in range(1, segments + 1):
        checkpoint["status"] = "running"
        checkpoint["current_segment"] = index
        checkpoint["skill_generation_current"] = int(
            getattr(agent.registry, "runtime_generation", 0) or 0
        )
        checkpoint["runtime_instance"] = str(
            agent._task_continuation_instance
        )
        checkpoint["owner_pid"] = os.getpid()
        store.add_event(
            checkpoint, "segment_started", f"segment={index}/{segments}"
        )
        store.write_checkpoint(root, checkpoint)
        store.set_active(root, checkpoint)
        agent._task_continuation_active_id = checkpoint["id"]
        prompt = (
            first_prompt
            if index == 1
            else continuation_prompt(checkpoint, index, segments)
        )
        reply = original_chat(prompt, subject=subject)
        checkpoint["last_reply"] = store.safe_text(reply, 2000)
        store.add_event(
            checkpoint, "segment_finished", f"segment={index}/{segments}"
        )
        store.write_checkpoint(root, checkpoint)
        if MAX_ROUND_SENTINEL not in reply:
            return reply
    return reply


def checkpoint_reply(checkpoint: dict[str, Any], segments: int) -> str:
    return (
        "可恢复检查点\n"
        f"- ID：{checkpoint.get('id')}\n"
        f"- 原始目标：{checkpoint.get('original_goal')}\n"
        f"- 已自动续办：{segments} 个有界工具段\n"
        f"- 技能 generation：{checkpoint.get('skill_generation_current')}\n"
        f"- 受限目标：{json.dumps(checkpoint.get('scope', {}), ensure_ascii=False, sort_keys=True)}\n"
        "- 当前状态：总工具预算已耗尽，任务已持久化为 pending；进程重启后会幂等续办。"
    )


def resume_pending(
    agent: Any, original_chat: Any, segments: int, root: Path
) -> str:
    checkpoint = store.find_resumable(root)
    if not checkpoint:
        return ""
    attempts = int(checkpoint.get("attempts", 0) or 0)
    maximum = int(
        checkpoint.get("max_attempts", store.MAX_RESUME_ATTEMPTS)
        or store.MAX_RESUME_ATTEMPTS
    )
    if attempts >= maximum:
        checkpoint["status"] = "blocked"
        checkpoint["last_error"] = "自动恢复次数达到上限"
        store.add_event(checkpoint, "resume_blocked", f"attempts={attempts}")
        store.write_checkpoint(root, checkpoint)
        store.clear_active(root, str(checkpoint["id"]))
        return ""

    instance = str(agent._task_continuation_instance)
    if not store.acquire_lock(root, checkpoint, instance):
        return ""
    try:
        checkpoint["attempts"] = attempts + 1
        checkpoint["status"] = "running"
        store.add_event(
            checkpoint,
            "startup_resume",
            f"attempt={checkpoint['attempts']}",
        )
        store.write_checkpoint(root, checkpoint)
        try:
            reply = run_segments(
                agent=agent,
                original_chat=original_chat,
                checkpoint=checkpoint,
                subject=str(checkpoint.get("subject", "agent") or "agent"),
                segments=segments,
                root=root,
                first_prompt=continuation_prompt(checkpoint, 1, segments),
            )
        except Exception as exc:
            checkpoint["status"] = "pending"
            checkpoint["last_error"] = store.safe_text(
                f"{type(exc).__name__}: {exc}", 1000
            )
            store.add_event(
                checkpoint, "resume_failed", checkpoint["last_error"]
            )
            store.write_checkpoint(root, checkpoint)
            store.set_active(root, checkpoint)
            return ""

        if MAX_ROUND_SENTINEL in reply:
            checkpoint["status"] = "pending"
            store.add_event(
                checkpoint, "resume_budget_exhausted", f"segments={segments}"
            )
            store.write_checkpoint(root, checkpoint)
            store.set_active(root, checkpoint)
            return checkpoint_reply(checkpoint, segments)

        checkpoint["status"] = "completed"
        checkpoint["last_reply"] = store.safe_text(reply, 2000)
        checkpoint["completed_at"] = store.now_iso()
        store.add_event(
            checkpoint, "resume_completed", "进程重启后的续办已完成"
        )
        store.write_checkpoint(root, checkpoint)
        store.clear_active(root, str(checkpoint["id"]))
        try:
            agent.memory.add(
                "episode",
                f"任务检查点 {checkpoint['id']} 在进程重启后续办完成：{checkpoint['last_reply']}",
            )
        except Exception:
            pass
        return reply
    finally:
        agent._task_continuation_active_id = ""
        store.release_lock(root, str(checkpoint["id"]), instance)


def configure_runtime(
    *,
    agent: Any,
    registry: Any | None = None,
    config: dict[str, Any] | None = None,
    **_: Any,
) -> None:
    """Bind persistent continuity once to one Agent instance."""

    if getattr(agent, "_task_continuation_bound", False):
        return
    agent._task_continuation_bound = True
    agent._task_continuation_instance = (
        f"{os.environ.get('HOSTNAME', 'local')}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    )
    agent._task_continuation_active_id = ""
    root = store.runtime_root()
    segments = registry_runtime.segment_budget(config)

    registry_runtime.bind_generation(registry, root)
    registry_runtime.bind_fresh_model_context(agent, registry)
    if registry is not None:
        registry_runtime.bind_dispatch_scope(registry, root)

    original_refresh = agent._refresh_system_prompt

    def refresh_with_continuity(self: Any) -> None:
        original_refresh()
        generation = int(
            getattr(self.registry, "runtime_generation", 0) or 0
        )
        if PROMPT_MARKER not in self.system_prompt:
            self.system_prompt += (
                "\n\n"
                f"{PROMPT_MARKER}\n"
                f"- 当前技能 runtime generation={generation}；每次模型调用必须读取最新工具清单。\n"
                "- 为当前任务新增、升级、晋升或重载技能时，技能变更只是中间步骤；必须继续最初目标。\n"
                "- 对话开始前会建立持久检查点；进程或容器重启后，检查点通过租约锁幂等恢复。\n"
                "- 主人对 Docker 技能升级的明确授权只在同一服务器配置身份和固定 Docker 技能文件范围内继承；目标或 SSH 身份变化时必须停止继承。\n"
                "- 该授权永远不替代 docker_restart、Compose 部署等外部变更的 exact approval。\n"
                "- 只有任务完成、等待具体外部审批，或存在无法自动消除的外部阻塞时才结束。"
            )

    agent._refresh_system_prompt = MethodType(
        refresh_with_continuity, agent
    )
    original_chat = agent.chat

    def chat_with_continuation(
        self: Any, user_input: str, *, subject: str = "agent"
    ) -> str:
        authorization = scope.authorization_record(str(user_input))
        initial_scope = (
            scope.load_last_scope(root) if authorization["granted"] else {}
        )
        checkpoint = store.new_checkpoint(
            agent=self,
            goal=str(user_input),
            subject=subject,
            root=root,
            authorization=authorization,
            initial_scope=initial_scope,
        )
        instance = str(self._task_continuation_instance)
        if not store.acquire_lock(root, checkpoint, instance):
            checkpoint["status"] = "blocked"
            checkpoint["last_error"] = "无法获得任务续跑租约锁"
            store.add_event(
                checkpoint, "lock_failed", checkpoint["last_error"]
            )
            store.write_checkpoint(root, checkpoint)
            return "任务未执行：无法获得任务续跑租约锁，请检查重复 Agent 实例。"
        try:
            try:
                reply = run_segments(
                    agent=self,
                    original_chat=original_chat,
                    checkpoint=checkpoint,
                    subject=subject,
                    segments=segments,
                    root=root,
                    first_prompt=str(user_input),
                )
            except Exception as exc:
                checkpoint["status"] = "pending"
                checkpoint["last_error"] = store.safe_text(
                    f"{type(exc).__name__}: {exc}", 1000
                )
                store.add_event(
                    checkpoint, "chat_interrupted", checkpoint["last_error"]
                )
                store.write_checkpoint(root, checkpoint)
                store.set_active(root, checkpoint)
                raise

            if MAX_ROUND_SENTINEL in reply:
                checkpoint["status"] = "pending"
                store.add_event(
                    checkpoint,
                    "total_budget_exhausted",
                    f"segments={segments}",
                )
                store.write_checkpoint(root, checkpoint)
                store.set_active(root, checkpoint)
                text = checkpoint_reply(checkpoint, segments)
                try:
                    self.memory.add("episode", text)
                except Exception:
                    pass
                return text

            checkpoint["status"] = "completed"
            checkpoint["last_reply"] = store.safe_text(reply, 2000)
            checkpoint["completed_at"] = store.now_iso()
            store.add_event(checkpoint, "completed", "本轮任务已完成")
            store.write_checkpoint(root, checkpoint)
            store.clear_active(root, str(checkpoint["id"]))
            return reply
        finally:
            self._task_continuation_active_id = ""
            store.release_lock(root, str(checkpoint["id"]), instance)

    agent.chat = MethodType(chat_with_continuation, agent)
    agent._refresh_system_prompt()
    agent.startup_resume_result = resume_pending(
        agent, original_chat, segments, root
    )
