"""Fail-fast scope classification for controlled self-evolution.

Some goals necessarily touch runners, policy, Docker topology, CI or approval logic.
Those files are intentionally protected from the autonomous sandbox.  Repeatedly
trying the sandbox cannot succeed and previously burned dozens of model rounds.  This
runtime wrapper returns an auditable host-review result before any candidate is built.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import MethodType
from typing import Any

SKILL_META = {
    "name": "evolution_scope_guard",
    "description": (
        "在自主迭代前识别 Runner、Docker 拓扑、审批、策略和 CI 等宿主机控制面目标，"
        "立即转为人类主导仓库变更，避免无效循环。"
    ),
    "version": "1.0.0",
}

CAPABILITY_META = {
    "id": "agent.evolution_scope_guard",
    "name": "自我迭代范围闸门",
    "description": "对受保护控制面目标失败关闭并生成可审计结果。",
    "version": "1.0.0",
    "domain": "governance",
    "operations": [],
    "composes_with": ["agent.evolution", "agent.self_development", "agent.task_continuation"],
}

TOOLS: list[dict[str, Any]] = []

_HOST_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("runner", re.compile(r"(?i)\b(?:ops|approval|validation|repair)[-_ ]?runner\b|执行器|runner")),
    ("compose_lifecycle", re.compile(r"(?i)(?:docker\s+compose|compose)[-_ ]?(?:down|stop)|docker\s+down|容器编排.*(?:停止|下线)")),
    ("docker_topology", re.compile(r"(?i)docker-compose\.ya?ml|compose\s+拓扑|挂载(?:点|目录)|network_mode|docker\s+socket")),
    ("approval", re.compile(r"(?i)审批(?:逻辑|通道|权限)|authorization|approval|auth-decisions")),
    ("policy", re.compile(r"(?i)安全策略|权限策略|policy|execution[_ -]?policy|治理规则")),
    ("ci", re.compile(r"(?i)\.github/workflows|github actions|codeql|供应链|\bCI\b")),
    ("host_script", re.compile(r"(?i)scripts/|gate_check|promote\.sh|宿主机脚本")),
)


def classify_goal(goal: str) -> list[str]:
    text = str(goal or "")
    return [name for name, pattern in _HOST_PATTERNS if pattern.search(text)]


def _root(agent: Any) -> Path:
    configured = getattr(agent, "config", {}).get("runtime_root") or os.environ.get("AGENELF_ROOT")
    return Path(configured).resolve() if configured else Path(__file__).resolve().parents[2]


def _write_cycle(agent: Any, goal: str, matches: list[str]) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cycle_id = f"auto-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    value = {
        "schema_version": 1,
        "id": cycle_id,
        "started_at": now,
        "updated_at": now,
        "status": "host_review_required",
        "goal": goal,
        "apply_changes": False,
        "matched_host_controlled_scopes": matches,
        "error": (
            "目标涉及宿主机控制面或受保护文件，不能由 app-tmp 自主候选修改。"
            "应通过人类主导的仓库分支、完整 CI 和合并流程实现。"
        ),
        "next_action": "human_managed_repository_change",
    }
    path = _root(agent) / "data" / "autonomy-cycles" / f"{cycle_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
    return value


def configure_runtime(*, agent: Any, **_: Any) -> None:
    if getattr(agent, "_agenelf_evolution_scope_guard_bound", False):
        return
    original = getattr(agent, "run_autonomy_cycle", None)
    if not callable(original):
        return
    agent._agenelf_evolution_scope_guard_bound = True
    agent._agenelf_unscoped_autonomy_cycle = original

    def guarded(self: Any, goal: str = "", apply_changes: bool = False) -> dict[str, Any]:
        matches = classify_goal(goal)
        if apply_changes and matches:
            return _write_cycle(self, str(goal or "").strip(), matches)
        return original(goal=goal, apply_changes=apply_changes)

    agent.run_autonomy_cycle = MethodType(guarded, agent)


def execute(tool_name: str, args: dict[str, Any]) -> str:
    del tool_name, args
    return "evolution_scope_guard 是运行时治理能力，不暴露模型工具。"
