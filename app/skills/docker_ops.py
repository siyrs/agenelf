"""Structured remote Docker operations backed by the deterministic SSH runner.

The LLM-facing Agent never receives SSH credentials and never builds arbitrary remote
shell commands. This skill only creates fingerprint-bound operation requests. The
host-side unified runner validates the same target, operation, container and policy
again before using the private SSH material.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import yaml

from core import operations

SKILL_META = {
    "name": "docker_ops",
    "description": (
        "通过隔离 SSH Runner 查看远程 Docker 日志和安全元数据、运行主人预配置的诊断检查，"
        "并以精确审批方式重启容器；支持 servers.yaml 热更新。"
    ),
    "version": "1.0.0",
}

CAPABILITY_META = {
    "id": "docker.operations",
    "name": "远程 Docker 运维",
    "description": (
        "面向已配置服务器的结构化 Docker 诊断与恢复能力。日志与 inspect 输出会脱敏；"
        "不开放任意 docker exec 或远程 Shell。"
    ),
    "version": "1.0.0",
    "domain": "operations",
    "operations": [
        {"name": "list_docker_runtime", "description": "查看目标 Docker 策略摘要", "risk": "read"},
        {"name": "get_docker_logs", "description": "读取容器最近日志", "risk": "read"},
        {"name": "inspect_docker_container", "description": "读取不含环境变量的容器元数据", "risk": "read"},
        {"name": "run_docker_check", "description": "运行主人预配置的只读容器诊断", "risk": "read"},
        {"name": "restart_docker_container", "description": "重启容器并读取新状态", "risk": "change"},
        {"name": "get_docker_operation", "description": "查询 Docker 运维请求结果", "risk": "read"},
    ],
    "composes_with": [
        "server.operations",
        "software.validation",
        "agent.workflow",
        "agent.task_continuation",
    ],
}

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_docker_runtime",
            "description": (
                "查看服务器 Docker 命令、允许的容器/操作及预配置诊断别名；"
                "不返回 SSH 凭据或诊断命令参数。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"target": {"type": "string"}},
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_docker_logs",
            "description": (
                "通过 SSH Runner 读取远程容器最近日志。输出会对常见 Token、密码、"
                "代理节点 URI 和订阅查询参数脱敏；只读，可自动执行。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "container": {"type": "string"},
                    "tail": {"type": "integer", "minimum": 1, "maximum": 1000},
                    "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 8},
                },
                "required": ["target", "container"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_docker_container",
            "description": (
                "读取远程容器状态、镜像、挂载、Compose 标签、重启策略和网络。"
                "明确排除 Config.Env，避免把容器环境变量中的秘密送入模型。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "container": {"type": "string"},
                    "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 8},
                },
                "required": ["target", "container"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_docker_check",
            "description": (
                "运行 local/servers.yaml 中 docker_checks 预先定义的诊断别名。"
                "模型只能选择别名，不能提交命令、参数或 Shell。只读，可自动执行。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "check": {"type": "string"},
                    "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 8},
                },
                "required": ["target", "check"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "restart_docker_container",
            "description": (
                "重启远程 Docker 容器并返回重启后的状态。会改变运行状态，"
                "只创建绑定目标与参数的请求，必须由主人批准后 Runner 执行。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "container": {"type": "string"},
                    "timeout_seconds": {"type": "integer", "minimum": 0, "maximum": 60},
                },
                "required": ["target", "container"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_docker_operation",
            "description": "查询 Docker 运维请求的排队、待批准、成功、失败或阻断状态。",
            "parameters": {
                "type": "object",
                "properties": {
                    "operation_id": {"type": "string"},
                    "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 8},
                },
                "required": ["operation_id"],
            },
        },
    },
]

_CONTAINER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_CHECK_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_REMOTE_RISKS = {
    "get_docker_logs": operations.RISK_READ,
    "inspect_docker_container": operations.RISK_READ,
    "run_docker_check": operations.RISK_READ,
    "restart_docker_container": operations.RISK_CHANGE,
}


def _root() -> Path:
    configured = os.environ.get("AGENELF_ROOT", "").strip()
    return Path(configured).resolve() if configured else Path(__file__).resolve().parents[2]


def _servers_path() -> Path:
    configured = os.environ.get("AGENELF_SERVERS_FILE", "").strip()
    return Path(configured).resolve() if configured else _root() / "local" / "servers.yaml"


def _load_servers() -> dict[str, dict[str, Any]]:
    path = _servers_path()
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    raw = data.get("servers", {}) if isinstance(data, dict) else {}
    if not isinstance(raw, dict):
        return {}
    return {str(name): cfg for name, cfg in raw.items() if isinstance(cfg, dict)}


def _profile(target: str) -> dict[str, Any]:
    target = str(target or "").strip()
    profiles = _load_servers()
    if target not in profiles:
        raise ValueError(f"未配置服务器 {target!r}；请检查 local/servers.yaml")
    return profiles[target]


def _allowed_operation(profile: dict[str, Any], operation: str) -> None:
    allowed = profile.get("allowed_docker_operations")
    if allowed is None:
        return
    if not isinstance(allowed, list) or operation not in {str(item) for item in allowed}:
        raise PermissionError(f"服务器 Docker 策略未允许操作：{operation}")


def _container(profile: dict[str, Any], raw: str) -> str:
    value = str(raw or "").strip()
    if not _CONTAINER_RE.fullmatch(value):
        raise ValueError("container 名称非法")
    allowed = profile.get("allowed_containers")
    if allowed is not None:
        if not isinstance(allowed, list) or value not in {str(item) for item in allowed}:
            raise PermissionError(f"容器 {value!r} 不在 allowed_containers 清单中")
    return value


def _check(profile: dict[str, Any], raw: str) -> tuple[str, dict[str, Any]]:
    alias = str(raw or "").strip()
    if not _CHECK_RE.fullmatch(alias):
        raise ValueError("check 别名非法")
    checks = profile.get("docker_checks", {})
    if not isinstance(checks, dict) or not isinstance(checks.get(alias), dict):
        raise PermissionError(f"未配置 Docker 诊断别名：{alias}")
    entry = checks[alias]
    _container(profile, str(entry.get("container", "")))
    argv = entry.get("argv")
    if not isinstance(argv, list) or not argv or len(argv) > 32:
        raise ValueError(f"Docker 诊断 {alias!r} 的 argv 必须是 1-32 项列表")
    for item in argv:
        text = str(item)
        if not text or len(text) > 500 or "\n" in text or "\x00" in text:
            raise ValueError(f"Docker 诊断 {alias!r} 含非法参数")
    return alias, entry


def _wait(value: Any, default: int = 3) -> int:
    try:
        return max(0, min(int(value), 8))
    except (TypeError, ValueError):
        return default


def _submit(
    target: str,
    operation: str,
    parameters: dict[str, Any],
    summary: str,
    wait_seconds: int = 0,
) -> str:
    profile = _profile(target)
    _allowed_operation(profile, operation)
    request = operations.submit_operation(
        capability="docker.operations",
        operation=operation,
        target=target,
        parameters=parameters,
        risk=_REMOTE_RISKS[operation],
        summary=summary,
    )
    if request["risk"] == operations.RISK_READ:
        state = operations.wait_for_result(
            request["id"], timeout_seconds=_wait(wait_seconds, 0)
        )
        return json.dumps(state, ensure_ascii=False, indent=2)
    return (
        f"Docker 运维请求已创建：{request['id']}\n"
        f"风险级别：{request['risk']}\n"
        f"目标：{target}\n"
        f"操作：{operation}\n"
        f"摘要：{summary}\n"
        f"批准命令：bash scripts/approve.sh {request['id']} approve\n"
        "批准只绑定本次目标、容器和参数；Runner 会在执行前再次校验。"
    )


def list_docker_runtime(target: str) -> str:
    try:
        profile = _profile(target)
        checks = profile.get("docker_checks", {})
        check_items: list[dict[str, str]] = []
        if isinstance(checks, dict):
            for alias, entry in sorted(checks.items()):
                if isinstance(entry, dict):
                    check_items.append(
                        {"name": str(alias), "container": str(entry.get("container", ""))}
                    )
        value = {
            "target": target,
            "host": profile.get("host"),
            "docker_command": profile.get("docker_command", "docker"),
            "allowed_docker_operations": profile.get(
                "allowed_docker_operations", sorted(_REMOTE_RISKS)
            ),
            "allowed_containers": profile.get("allowed_containers", "all-valid-names"),
            "docker_checks": check_items,
            "profiles_reload": "runner reloads servers.yaml before every queue scan",
        }
        return json.dumps(value, ensure_ascii=False, indent=2)
    except (ValueError, PermissionError) as exc:
        return f"查询失败：{exc}"


def get_docker_logs(
    target: str, container: str, tail: int = 100, wait_seconds: int = 3
) -> str:
    try:
        profile = _profile(target)
        container = _container(profile, container)
        tail = max(1, min(int(tail), 1000))
        return _submit(
            target,
            "get_docker_logs",
            {"container": container, "tail": tail},
            f"读取 {target}/{container} 最近 {tail} 行日志",
            wait_seconds,
        )
    except (TypeError, ValueError, PermissionError) as exc:
        return f"操作失败：{exc}"


def inspect_docker_container(
    target: str, container: str, wait_seconds: int = 3
) -> str:
    try:
        profile = _profile(target)
        container = _container(profile, container)
        return _submit(
            target,
            "inspect_docker_container",
            {"container": container},
            f"读取 {target}/{container} 的安全 Docker 元数据",
            wait_seconds,
        )
    except (ValueError, PermissionError) as exc:
        return f"操作失败：{exc}"


def run_docker_check(target: str, check: str, wait_seconds: int = 3) -> str:
    try:
        profile = _profile(target)
        alias, entry = _check(profile, check)
        return _submit(
            target,
            "run_docker_check",
            {"check": alias},
            f"在 {target}/{entry.get('container')} 运行预配置诊断 {alias}",
            wait_seconds,
        )
    except (ValueError, PermissionError) as exc:
        return f"操作失败：{exc}"


def restart_docker_container(
    target: str, container: str, timeout_seconds: int = 10
) -> str:
    try:
        profile = _profile(target)
        container = _container(profile, container)
        timeout_seconds = max(0, min(int(timeout_seconds), 60))
        return _submit(
            target,
            "restart_docker_container",
            {"container": container, "timeout_seconds": timeout_seconds},
            f"重启 {target}/{container}（停止超时 {timeout_seconds}s）",
        )
    except (TypeError, ValueError, PermissionError) as exc:
        return f"操作失败：{exc}"


def get_docker_operation(operation_id: str, wait_seconds: int = 0) -> str:
    try:
        state = operations.wait_for_result(
            operation_id, timeout_seconds=_wait(wait_seconds, 0)
        )
    except (TypeError, ValueError) as exc:
        return f"查询失败：{exc}"
    return json.dumps(state, ensure_ascii=False, indent=2)


_DISPATCH = {
    "list_docker_runtime": lambda a: list_docker_runtime(a.get("target", "")),
    "get_docker_logs": lambda a: get_docker_logs(
        a.get("target", ""),
        a.get("container", ""),
        a.get("tail", 100),
        a.get("wait_seconds", 3),
    ),
    "inspect_docker_container": lambda a: inspect_docker_container(
        a.get("target", ""), a.get("container", ""), a.get("wait_seconds", 3)
    ),
    "run_docker_check": lambda a: run_docker_check(
        a.get("target", ""), a.get("check", ""), a.get("wait_seconds", 3)
    ),
    "restart_docker_container": lambda a: restart_docker_container(
        a.get("target", ""), a.get("container", ""), a.get("timeout_seconds", 10)
    ),
    "get_docker_operation": lambda a: get_docker_operation(
        a.get("operation_id", ""), a.get("wait_seconds", 0)
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
