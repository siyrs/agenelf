"""Bounded Docker diagnostics and recovery through the isolated SSH runner.

This skill deliberately exposes only exact container names and structured operations.
It never accepts shell fragments, never runs ``docker exec`` and never mounts the
Docker socket into the Agent process.
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
    from core import operations
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from core import operations

SKILL_META = {
    "name": "docker_ops",
    "description": "通过隔离 SSH Runner 读取容器日志、执行诊断并按精确审批重启容器。",
    "version": "1.0.0",
}

CAPABILITY_META = {
    "id": "server.docker",
    "name": "Docker 运维",
    "description": "有界日志、无环境变量值的容器诊断，以及需要精确批准的容器重启。",
    "version": "1.0.0",
    "domain": "operations",
    "composes_with": ["server.operations", "software.validation", "agent.self_development"],
    "operations": [
        {"name": "docker_logs", "description": "读取指定容器的有界日志", "risk": "read"},
        {"name": "docker_diagnose", "description": "汇总容器状态、日志与资源快照", "risk": "read"},
        {"name": "docker_restart", "description": "重启指定容器并验证状态", "risk": "change"},
    ],
}

_CONTAINER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_MAX_TAIL = 2000
_RISKS = {
    "docker_logs": operations.RISK_READ,
    "docker_diagnose": operations.RISK_READ,
    "docker_restart": operations.RISK_CHANGE,
}

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "docker_logs",
            "description": (
                "读取目标服务器上指定 Docker 容器最近的日志。只读并自动执行；"
                "适合排查启动失败、配置错误和反复重启。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "servers.yaml 中的服务器别名"},
                    "container": {"type": "string", "description": "精确容器名称或 ID"},
                    "tail": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
                    "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 8, "default": 3},
                },
                "required": ["target", "container"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "docker_diagnose",
            "description": (
                "对指定容器执行只读诊断：状态摘要、最近日志和一次性资源快照。"
                "不会读取环境变量值，也不会执行容器内命令。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "container": {"type": "string", "description": "精确容器名称或 ID"},
                    "tail": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 200},
                    "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 8, "default": 5},
                },
                "required": ["target", "container"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "docker_restart",
            "description": (
                "重启精确指定的 Docker 容器并验证状态。属于外部系统变更，"
                "只创建绑定目标和参数的请求，必须由宿主机审批后才会执行。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "container": {"type": "string", "description": "精确容器名称或 ID"},
                },
                "required": ["target", "container"],
            },
        },
    },
]


def _root() -> Path:
    configured = os.environ.get("AGENELF_ROOT", "").strip()
    return Path(configured).resolve() if configured else Path(__file__).resolve().parents[2]


def _servers_path() -> Path:
    configured = os.environ.get("AGENELF_SERVERS_FILE", "").strip()
    return Path(configured).resolve() if configured else _root() / "config" / "servers.yaml"


def _load_profiles() -> dict[str, dict[str, Any]]:
    path = _servers_path()
    if not path.is_file():
        return {}
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    raw = document.get("servers", {}) if isinstance(document, dict) else {}
    if not isinstance(raw, dict):
        return {}
    return {str(name): value for name, value in raw.items() if isinstance(value, dict)}


def _profile(target: str) -> dict[str, Any]:
    name = str(target or "").strip()
    profile = _load_profiles().get(name)
    if profile is None:
        raise ValueError(f"未配置服务器 {name!r}")
    return profile


def _allowed(profile: dict[str, Any], operation: str) -> None:
    raw = profile.get("allowed_operations")
    if raw is None:
        return
    if not isinstance(raw, list):
        raise PermissionError(f"服务器策略未允许操作：{operation}")
    names = {str(item) for item in raw}
    if operation in names:
        return
    if operation in {"docker_logs", "docker_diagnose"} and "docker_ps" in names:
        return
    # Legacy profiles often granted Docker visibility plus restart capability before
    # container-level restart existed. Require both grants and still require the exact
    # host-side request approval; either grant alone is insufficient.
    if operation == "docker_restart" and {"docker_ps", "service_restart"} <= names:
        return
    raise PermissionError(f"服务器策略未允许操作：{operation}")


def _container(value: object) -> str:
    name = str(value or "").strip()
    if not _CONTAINER_RE.fullmatch(name):
        raise ValueError("container 只能是字母或数字开头，并包含字母、数字、点、下划线和短横线")
    return name


def _tail(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("tail 必须是整数")
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("tail 必须是整数") from exc
    if count < 1 or count > _MAX_TAIL:
        raise ValueError(f"tail 必须在 1 到 {_MAX_TAIL} 之间")
    return count


def _submit(
    target: str,
    operation: str,
    parameters: dict[str, Any],
    summary: str,
    wait_seconds: int = 0,
) -> str:
    profile = _profile(target)
    _allowed(profile, operation)
    request = operations.submit_operation(
        capability="server.docker",
        operation=operation,
        target=str(target).strip(),
        parameters=parameters,
        risk=_RISKS[operation],
        summary=summary,
    )
    if request["risk"] == operations.RISK_READ:
        state = operations.wait_for_result(
            request["id"], timeout_seconds=max(0, min(int(wait_seconds), 8))
        )
        return json.dumps(state, ensure_ascii=False, indent=2)
    return (
        f"Docker 运维请求已创建：{request['id']}\n"
        f"风险级别：{request['risk']}\n"
        f"目标：{target}\n"
        f"操作：{operation}\n"
        f"摘要：{summary}\n"
        f"批准命令：bash scripts/approve.sh {request['id']} approve\n"
        "批准只绑定本请求的服务器、容器和操作；参数变化后必须重新申请。"
    )


def docker_logs(
    target: str, container: str, tail: int = 200, wait_seconds: int = 3
) -> str:
    try:
        safe_container = _container(container)
        safe_tail = _tail(tail)
        return _submit(
            target,
            "docker_logs",
            {"container": safe_container, "tail": safe_tail},
            f"读取 {target} 上容器 {safe_container} 最近 {safe_tail} 行日志",
            wait_seconds,
        )
    except (ValueError, PermissionError) as exc:
        return f"Docker 日志请求失败：{exc}"


def docker_diagnose(
    target: str, container: str, tail: int = 200, wait_seconds: int = 5
) -> str:
    try:
        safe_container = _container(container)
        safe_tail = _tail(tail)
        return _submit(
            target,
            "docker_diagnose",
            {"container": safe_container, "tail": safe_tail},
            f"诊断 {target} 上 Docker 容器 {safe_container}",
            wait_seconds,
        )
    except (ValueError, PermissionError) as exc:
        return f"Docker 诊断请求失败：{exc}"


def docker_restart(target: str, container: str) -> str:
    try:
        safe_container = _container(container)
        return _submit(
            target,
            "docker_restart",
            {"container": safe_container},
            f"重启 {target} 上 Docker 容器 {safe_container} 并验证状态",
        )
    except (ValueError, PermissionError) as exc:
        return f"Docker 重启请求失败：{exc}"


_DISPATCH = {
    "docker_logs": lambda args: docker_logs(
        args.get("target", ""),
        args.get("container", ""),
        args.get("tail", 200),
        args.get("wait_seconds", 3),
    ),
    "docker_diagnose": lambda args: docker_diagnose(
        args.get("target", ""),
        args.get("container", ""),
        args.get("tail", 200),
        args.get("wait_seconds", 5),
    ),
    "docker_restart": lambda args: docker_restart(
        args.get("target", ""), args.get("container", "")
    ),
}


def execute(tool_name: str, args: dict[str, Any]) -> str:
    handler = _DISPATCH.get(tool_name)
    if handler is None:
        return f"未知工具：{tool_name}"
    try:
        return str(handler(args or {}))
    except Exception as exc:
        return f"执行失败：{type(exc).__name__}: {exc}"
