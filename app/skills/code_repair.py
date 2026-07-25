"""Owner-configured isolated code repair capability.

The Agent can list safe repository aliases, submit an exact unified diff and query
trusted evidence.  Source repositories are never mounted into the Agent container;
a separate network-disabled runner works on a disposable copy and never commits,
pushes or merges.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from core import code_repair

SKILL_META = {
    "name": "code_repair",
    "description": "对主人配置的 Git 仓库别名提交统一补丁，由无网络 repair-runner 在只读源码副本上应用并运行测试。",
    "version": "1.0.0",
}

CAPABILITY_META = {
    "id": "code.repair",
    "name": "隔离代码修复",
    "description": (
        "在一次性 Git 副本中应用指纹绑定的 unified diff，执行主人预配置测试并保存可信证据；"
        "不会修改源仓库、提交、推送或合并。"
    ),
    "version": "1.0.0",
    "domain": "development",
    "operations": [
        {"name": "catalog", "description": "列出可修复仓库别名和测试配置，不暴露主机路径", "risk": "read"},
        {"name": "submit_patch", "description": "在隔离副本应用补丁并测试，不改变源仓库", "risk": "read"},
        {"name": "get_result", "description": "查询可信修复证据", "risk": "read"},
    ],
    "composes_with": [
        "agent.workflow",
        "agent.self_development",
        "software.validation",
        "software.release",
    ],
}

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_code_repair_repositories",
            "description": "列出 local/repositories.yaml 中的仓库别名、语言和允许的测试配置；隐藏真实路径和命令。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_code_repair_patch",
            "description": (
                "向隔离 repair-runner 提交一个标准 git unified diff。Runner 在只读源码的临时副本中应用补丁并运行主人预配置测试；"
                "不会修改源仓库、commit、push 或 merge。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repository": {"type": "string", "description": "仓库别名。"},
                    "unified_diff": {"type": "string", "description": "完整 git unified diff。"},
                    "test_profile": {"type": "string", "description": "可选测试配置别名；为空时使用仓库默认值。"},
                    "expected_base": {"type": "string", "description": "可选 7-64 位 Git commit SHA，防止在错误基线上修复。"},
                    "summary": {"type": "string", "description": "修复目标摘要，不得包含凭据。"},
                    "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 15, "description": "同步等待结果秒数，默认 5。"},
                },
                "required": ["repository", "unified_diff"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_code_repair_result",
            "description": "查询 repair- 开头请求的队列状态和可信测试证据。",
            "parameters": {
                "type": "object",
                "properties": {
                    "repair_id": {"type": "string"},
                    "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 15},
                },
                "required": ["repair_id"],
            },
        },
    },
]

_AGENT: Any | None = None


def configure_runtime(*, agent: Any = None, **_: Any) -> None:
    global _AGENT
    _AGENT = agent


def _root() -> Path:
    configured = os.environ.get("AGENELF_ROOT", "").strip()
    return Path(configured).resolve() if configured else Path(__file__).resolve().parents[2]


def _config() -> dict[str, Any]:
    return code_repair.load_repair_config()


def _catalog() -> dict[str, Any]:
    return code_repair.safe_catalog(_config())


def _repository(alias: str) -> tuple[dict[str, Any], str]:
    config = _config()
    repositories = config.get("repositories", {})
    profile = repositories.get(alias) if isinstance(repositories, dict) else None
    if not isinstance(profile, dict):
        raise ValueError(f"未知代码仓库别名：{alias}")
    allowed = profile.get("allowed_test_profiles", [])
    if not isinstance(allowed, list) or not allowed:
        raise ValueError(f"仓库 {alias} 没有可用测试配置")
    default = str(profile.get("default_test_profile", allowed[0]))
    return profile, default


def _wait(value: object, default: int = 5) -> int:
    try:
        return max(0, min(int(value), 15))
    except (TypeError, ValueError):
        return default


def _observe(state: dict[str, Any]) -> None:
    if _AGENT is None or not isinstance(state.get("result"), dict):
        return
    result = state["result"]
    if str(result.get("status")) != "failed":
        return
    repository = str(result.get("repository") or "unknown")
    summary = str(result.get("summary") or "代码修复测试失败")[:1000]
    try:
        _AGENT.create_improvement_intention(
            title=f"分析并修复代码修复失败：{repository}",
            rationale=summary,
            priority="P1",
            acceptance_criteria=[
                "在隔离 repair-runner 中重新应用补丁",
                "主人配置的完整测试通过",
                "保留 repair- 可信证据",
                "不绕过源码只读、无网络和不自动合并边界",
            ],
        )
        _AGENT.reflect_and_sediment(
            note=f"代码修复 {state.get('id')} 在仓库 {repository} 上失败：{summary}",
            deep=False,
        )
    except Exception:
        pass


def _submit(args: dict[str, Any]) -> dict[str, Any]:
    alias = str(args.get("repository", "")).strip()
    profile, default = _repository(alias)
    test_profile = str(args.get("test_profile", "") or default).strip()
    allowed = profile.get("allowed_test_profiles", [])
    if test_profile not in {str(item) for item in allowed}:
        raise ValueError(f"仓库 {alias} 未允许测试配置 {test_profile}")
    request = code_repair.submit_repair(
        alias,
        str(args.get("unified_diff", "")),
        test_profile,
        str(args.get("summary", "")),
        expected_base=str(args.get("expected_base", "")),
        root=_root(),
    )
    state = code_repair.wait_for_repair(
        request["id"],
        timeout_seconds=_wait(args.get("wait_seconds")),
        root=_root(),
    )
    _observe(state)
    return state


def execute(tool_name: str, args: dict) -> str:
    data = args or {}
    try:
        if tool_name == "list_code_repair_repositories":
            return json.dumps(_catalog(), ensure_ascii=False, indent=2)
        if tool_name == "submit_code_repair_patch":
            return json.dumps(_submit(data), ensure_ascii=False, indent=2)
        if tool_name == "get_code_repair_result":
            state = code_repair.wait_for_repair(
                str(data.get("repair_id", "")),
                timeout_seconds=_wait(data.get("wait_seconds"), 0),
                root=_root(),
            )
            _observe(state)
            return json.dumps(state, ensure_ascii=False, indent=2)
        return f"未知工具：{tool_name}"
    except Exception as exc:
        return f"执行失败：{type(exc).__name__}: {exc}"
