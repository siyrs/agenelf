"""Composable deterministic software-validation capability.

The Agent selects only aliases declared by the owner in ``local/validation.yaml``.
Network endpoints never come from model-generated arguments.  A separate runner
performs HTTP/TCP checks and writes trusted evidence.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml

try:
    from core import validation
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from core import validation

SKILL_META = {
    "name": "software_validation",
    "description": "通过隔离的确定性执行器运行主人配置的 HTTP/TCP 验证与冒烟测试。",
    "version": "1.0.0",
}

CAPABILITY_META = {
    "id": "software.validation",
    "name": "软件验证",
    "description": "运行 allowlist 中的 HTTP/TCP 检查与验证套件，保留结构化证据，并把失败反馈给自我改进系统。",
    "version": "1.0.0",
    "domain": "quality",
    "operations": [
        {"name": "catalog", "description": "列出验证别名和套件，不暴露内部端点", "risk": "read"},
        {"name": "run_check", "description": "运行一个主人配置的验证检查", "risk": "read"},
        {"name": "run_suite", "description": "运行一个主人配置的验证套件", "risk": "read"},
        {"name": "get_result", "description": "查询验证状态与可信证据", "risk": "read"},
    ],
    "composes_with": [
        "server.operations",
        "agent.self_development",
        "code.repair",
        "software.release",
    ],
}

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_validation_checks",
            "description": "列出 local/validation.yaml 中的检查别名、类型、说明和套件；隐藏 URL、主机和端口。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_validation_check",
            "description": "运行一个主人预配置的 HTTP/TCP 检查。只读，由隔离 validation-runner 执行。",
            "parameters": {
                "type": "object",
                "properties": {
                    "check": {"type": "string", "description": "验证检查别名"},
                    "wait_seconds": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 8,
                        "description": "等待结果秒数，默认 3",
                    },
                },
                "required": ["check"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_validation_suite",
            "description": "运行一个主人预配置的验证套件并聚合全部检查证据。只读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "suite": {"type": "string", "description": "验证套件别名"},
                    "wait_seconds": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 12,
                        "description": "等待结果秒数，默认 5",
                    },
                },
                "required": ["suite"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_validation_result",
            "description": "查询 val- 开头的验证请求状态与可信结果。",
            "parameters": {
                "type": "object",
                "properties": {
                    "validation_id": {"type": "string"},
                    "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 8},
                },
                "required": ["validation_id"],
            },
        },
    },
]

_ALIAS_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_AGENT: Any | None = None


def configure_runtime(*, agent: Any, **_: Any) -> None:
    global _AGENT
    _AGENT = agent


def _root() -> Path:
    configured = os.environ.get("AGENELF_ROOT", "").strip()
    return Path(configured).resolve() if configured else Path(__file__).resolve().parents[2]


def _validation_path() -> Path:
    configured = os.environ.get("AGENELF_VALIDATION_FILE", "").strip()
    if configured:
        return Path(configured).resolve()
    local_dir = os.environ.get("AGENELF_LOCAL_DIR", "").strip()
    return (Path(local_dir).resolve() if local_dir else _root() / "local") / "validation.yaml"


def _load() -> dict[str, Any]:
    path = _validation_path()
    if not path.is_file() or path.is_symlink():
        return {"checks": {}, "suites": {}}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {"checks": {}, "suites": {}}
    if not isinstance(data, dict):
        return {"checks": {}, "suites": {}}
    checks = data.get("checks", {})
    suites = data.get("suites", {})
    return {
        "checks": checks if isinstance(checks, dict) else {},
        "suites": suites if isinstance(suites, dict) else {},
    }


def _catalog() -> dict[str, Any]:
    data = _load()
    checks = []
    for name, cfg in sorted(data["checks"].items()):
        if not isinstance(cfg, dict) or not _ALIAS_RE.fullmatch(str(name)):
            continue
        checks.append(
            {
                "name": str(name),
                "type": str(cfg.get("type", "unknown")),
                "description": str(cfg.get("description", ""))[:500],
                "tags": [str(item) for item in cfg.get("tags", [])[:10]]
                if isinstance(cfg.get("tags", []), list)
                else [],
            }
        )
    suites = []
    for name, cfg in sorted(data["suites"].items()):
        if not _ALIAS_RE.fullmatch(str(name)):
            continue
        if isinstance(cfg, list):
            members = cfg
            description = ""
        elif isinstance(cfg, dict):
            members = cfg.get("checks", [])
            description = str(cfg.get("description", ""))[:500]
        else:
            continue
        suites.append(
            {
                "name": str(name),
                "description": description,
                "checks": [str(item) for item in members[:50]]
                if isinstance(members, list)
                else [],
            }
        )
    return {
        "config_present": _validation_path().is_file(),
        "checks": checks,
        "suites": suites,
    }


def _known(kind: str, alias: str) -> bool:
    catalog = _catalog()
    key = "checks" if kind == "check" else "suites"
    return alias in {str(item.get("name")) for item in catalog[key]}


def _wait(value: object, default: int, maximum: int) -> int:
    try:
        return max(0, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def _observe(state: dict[str, Any]) -> None:
    if _AGENT is None or not isinstance(state.get("result"), dict):
        return
    result = state["result"]
    if str(result.get("status")) != "failed":
        return
    request = state.get("request") if isinstance(state.get("request"), dict) else {}
    target = str(request.get("target") or result.get("target") or "unknown")
    summary = str(result.get("summary") or result.get("reason") or "验证失败")[:1000]
    try:
        _AGENT.create_improvement_intention(
            title=f"分析并修复软件验证失败：{target}",
            rationale=summary,
            priority="P1",
            acceptance_criteria=[
                f"验证检查或套件 {target} 恢复通过",
                "增加可复现的回归验证",
                "保留 validation-runner 产生的可信证据",
                "不绕过网络、凭据、测试或晋升安全边界",
            ],
        )
        _AGENT.reflect_and_sediment(
            note=f"软件验证 {target} 失败；证据 {state.get('id')}；摘要：{summary}",
            deep=False,
        )
    except Exception:
        # Validation evidence remains authoritative even if sedimentation is unavailable.
        pass


def _submit(operation: str, target: str, wait_seconds: int) -> dict[str, Any]:
    request = validation.submit_validation(
        operation,
        target,
        f"运行验证 {'检查' if operation == 'run_check' else '套件'} {target}",
        root=_root(),
    )
    state = validation.wait_for_validation(
        request["id"],
        timeout_seconds=wait_seconds,
        root=_root(),
    )
    _observe(state)
    return state


def execute(tool_name: str, args: dict) -> str:
    args = args or {}
    try:
        if tool_name == "list_validation_checks":
            return json.dumps(_catalog(), ensure_ascii=False, indent=2)
        if tool_name == "run_validation_check":
            alias = str(args.get("check", "")).strip()
            if not _ALIAS_RE.fullmatch(alias) or not _known("check", alias):
                return f"验证检查不存在或别名非法：{alias!r}"
            state = _submit("run_check", alias, _wait(args.get("wait_seconds"), 3, 8))
            return json.dumps(state, ensure_ascii=False, indent=2)
        if tool_name == "run_validation_suite":
            alias = str(args.get("suite", "")).strip()
            if not _ALIAS_RE.fullmatch(alias) or not _known("suite", alias):
                return f"验证套件不存在或别名非法：{alias!r}"
            state = _submit("run_suite", alias, _wait(args.get("wait_seconds"), 5, 12))
            return json.dumps(state, ensure_ascii=False, indent=2)
        if tool_name == "get_validation_result":
            validation_id = str(args.get("validation_id", "")).strip()
            state = validation.wait_for_validation(
                validation_id,
                timeout_seconds=_wait(args.get("wait_seconds"), 0, 8),
                root=_root(),
            )
            _observe(state)
            return json.dumps(state, ensure_ascii=False, indent=2)
        return f"未知工具：{tool_name}"
    except Exception as exc:
        return f"执行失败：{type(exc).__name__}: {exc}"
