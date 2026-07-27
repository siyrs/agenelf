#!/usr/bin/env python3
"""Resume one owner-authorized task checkpoint before opening the interactive CLI."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable

from core.agent import Agent
from core.configuration import load_config
from skills import task_continuation

APP_DIR = Path(__file__).resolve().parent


def run_once(
    *,
    agent_factory: Callable[[dict[str, Any]], Any] | None = None,
    config_loader: Callable[..., dict[str, Any]] | None = None,
    emit: Callable[[str], None] = print,
) -> int:
    checkpoint = task_continuation.claim_pending()
    if checkpoint is None:
        return 0

    continuation_id = str(checkpoint["id"])
    summary = str(checkpoint.get("task_summary", ""))
    prompt = str(checkpoint.get("resume_prompt", ""))
    emit(f"[resume] 检测到续跑任务 {continuation_id}：{summary}")

    loader = config_loader or load_config
    factory = agent_factory or Agent
    try:
        config = loader(app_dir=APP_DIR)
        agent = factory(config)
        resume_message = (
            "这是重启后的受控任务续跑。该检查点由主人在重启前显式建立。\n"
            f"continuation_id: {continuation_id}\n"
            f"原任务摘要：{summary}\n"
            f"续跑指令：{prompt}\n\n"
            "请从检查点继续，不重复已经有可信证据的步骤，不扩大原任务范围。"
            "只读诊断可继续；任何新的远程变更仍必须走结构化操作队列和精确审批。"
            "原任务真实完成后，调用 complete_task_continuation 并附上证据。"
        )
        reply = str(agent.chat(resume_message, subject="cli"))
        emit("[resume] Agenelf 续跑结果：")
        emit(reply)
        # The resumed Agent may have completed/cancelled the checkpoint via a tool.
        current = task_continuation.status()
        if current.get("id") == continuation_id and current.get("status") not in {
            "completed",
            "cancelled",
        }:
            task_continuation.finish_attempt(continuation_id, result=reply)
        return 0
    except Exception as exc:
        task_continuation.finish_attempt(
            continuation_id, error=f"{type(exc).__name__}: {exc}"
        )
        emit(f"[resume] 续跑失败：{type(exc).__name__}: {exc}")
        return 1


def main() -> int:
    return run_once()


if __name__ == "__main__":
    raise SystemExit(main())
