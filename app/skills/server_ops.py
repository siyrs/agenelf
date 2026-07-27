"""Composable server-operations capability.

The Agent never owns SSH keys and never executes remote shell directly. Every request
is written to the operation queue and a deterministic ``ops-runner`` performs the actual
SSH work. Read-only requests can run automatically; changes require an exact owner
approval. Identical unfinished operations reuse one expiring request ID.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

try:
    from core import operations, permissions
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from core import operations, permissions

SKILL_META = {
    "name": "server_ops",
    "description": "通过隔离的 SSH 运维执行器管理服务器、Docker、APT 与 systemd 服务；支持请求过期和同载荷复用。",
    "version": "1.1.0",
}

CAPABILITY_META = {
    "id": "server.operations",
    "name": "服务器运维",
    "description": (
        "服务器巡检、APT 元数据更新、Docker 安装与容器查看、Compose 部署、"
        "systemd 服务查询/重启。可与验证、发布、代码修复能力组合。"
    ),
    "version": "1.1.0",
    "domain": "operations",
    "composes_with": ["software.validation", "software.release", "code.repair"],
    "operations": [
        {"name": "inspect", "description": "服务器健康巡检", "risk": "read"},
        {"name": "docker_ps", "description": "查看 Docker 容器", "risk": "read"},
        {"name": "service_status", "description": "查看 systemd 服务", "risk": "read"},
        {"name": "apt_update", "description": "更新 APT 软件索引", "risk": "change"},
        {"name": "compose_deploy", "description": "受管目录内部署 Compose 项目", "risk": "change"},
        {"name": "service_restart", "description": "重启允许清单中的服务", "risk": "change"},
        {"name": "docker_install", "description": "安装并启动 Docker", "risk": "privileged"},
    ],
}

_OPERATION_RISKS = {
    "inspect": operations.RISK_READ,
    "docker_ps": operations.RISK_READ,
    "service_status": operations.RISK_READ,
    "apt_update": operations.RISK_CHANGE,
    "compose_deploy": operations.RISK_CHANGE,
    "service_restart": operations.RISK_CHANGE,
    "docker_install": operations.RISK_PRIVILEGED,
}
_PROJECT_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}")
_SERVICE_RE = re.compile(r"[a-zA-Z0-9@_.-]{1,128}")

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_managed_servers",
            "description": "列出已配置的服务器别名和允许的运维操作，不返回密码或私钥。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_server",
            "description": "巡检服务器 CPU/内存/磁盘/系统与 Docker 状态。只读，可自动执行。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "servers.yaml 中的服务器别名"},
                    "wait_seconds": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 8,
                        "description": "等待执行器返回结果的秒数，默认 3",
                    },
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_docker_containers",
            "description": "查看目标服务器上的 Docker 容器。只读，可自动执行。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 8},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_apt_index",
            "description": "在目标服务器执行 apt-get update。会改变系统缓存，提交后需人类批准。",
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
            "name": "install_docker",
            "description": "通过 Ubuntu/Debian 仓库安装 Docker 与 Compose。高权限操作，必须人类批准。",
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
            "name": "deploy_compose_project",
            "description": (
                "将 Compose YAML 部署到服务器受管目录。禁止 privileged、host namespace、"
                "Docker Socket、设备映射和未授权绝对路径挂载；需人类批准。"
                "相同未完成请求会复用同一个 op ID。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "project": {"type": "string", "description": "项目名"},
                    "compose_yaml": {"type": "string", "description": "完整 Compose YAML"},
                    "pull": {"type": "boolean", "description": "部署前是否拉取镜像，默认 true"},
                    "plan_only": {"type": "boolean", "description": "仅校验并展示计划，不提交"},
                },
                "required": ["target", "project", "compose_yaml"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_system_service",
            "description": "查询或重启允许清单中的 systemd 服务；status 只读，restart 需批准。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "service": {"type": "string"},
                    "action": {"type": "string", "enum": ["status", "restart"]},
                    "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 8},
                },
                "required": ["target", "service", "action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_server_operation",
            "description": "查询运维请求的排队、待批准、执行成功、失败、过期或阻断状态。",
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


def _root() -> Path:
    configured = os.environ.get("AGENELF_ROOT", "").strip()
    return Path(configured).resolve() if configured else Path(__file__).resolve().parents[2]


def _servers_path() -> Path:
    configured = os.environ.get("AGENELF_SERVERS_FILE", "").strip()
    return Path(configured).resolve() if configured else _root() / "config" / "servers.yaml"


def load_servers() -> dict[str, dict[str, Any]]:
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
    profiles = load_servers()
    if target not in profiles:
        raise ValueError(f"未配置服务器 {target!r}；请检查 local/servers.yaml")
    return profiles[target]


def _allowed_operation(profile: dict[str, Any], operation: str) -> None:
    allowed = profile.get("allowed_operations")
    if allowed is None:
        return
    if not isinstance(allowed, list) or operation not in {str(item) for item in allowed}:
        raise PermissionError(f"服务器策略未允许操作：{operation}")


def _allowed_service(profile: dict[str, Any], service: str) -> None:
    if not _SERVICE_RE.fullmatch(service):
        raise ValueError("service 名称非法")
    allowed = profile.get("allowed_services", [])
    if not isinstance(allowed, list) or service not in {str(item) for item in allowed}:
        raise PermissionError(f"服务 {service!r} 不在 allowed_services 清单中")


def _is_under(path: str, roots: list[str]) -> bool:
    try:
        candidate = Path(path)
    except TypeError:
        return False
    if not candidate.is_absolute():
        return True
    normalized = candidate.as_posix().rstrip("/") or "/"
    for root in roots:
        root_value = Path(str(root)).as_posix().rstrip("/") or "/"
        if normalized == root_value or normalized.startswith(root_value + "/"):
            return True
    return False


def validate_compose(compose_yaml: str, profile: dict[str, Any]) -> dict[str, Any]:
    """Validate non-negotiable Compose red lines before queueing."""

    if not isinstance(compose_yaml, str) or not compose_yaml.strip():
        raise ValueError("compose_yaml 不能为空")
    try:
        document = yaml.safe_load(compose_yaml)
    except yaml.YAMLError as exc:
        raise ValueError(f"Compose YAML 解析失败：{exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("services"), dict):
        raise ValueError("Compose 必须包含 services 对象")
    if not document["services"]:
        raise ValueError("Compose services 不能为空")

    allowed_bind_roots = [str(item) for item in profile.get("allowed_bind_roots", [])]
    forbidden_socket = "/var/run/docker.sock"
    for service_name, service in document["services"].items():
        if not isinstance(service, dict):
            raise ValueError(f"service {service_name!r} 配置必须是对象")
        if service.get("privileged") is True:
            raise PermissionError(f"service {service_name!r} 禁止 privileged=true")
        for field in ("network_mode", "pid", "ipc", "userns_mode"):
            if str(service.get(field, "")).lower() == "host":
                raise PermissionError(f"service {service_name!r} 禁止 {field}: host")
        caps = service.get("cap_add", [])
        if isinstance(caps, list) and any(str(item).upper() == "ALL" for item in caps):
            raise PermissionError(f"service {service_name!r} 禁止 cap_add: ALL")
        if service.get("devices"):
            raise PermissionError(f"service {service_name!r} 禁止 devices 映射")

        volumes = service.get("volumes", [])
        if not isinstance(volumes, list):
            continue
        for volume in volumes:
            source = ""
            target = ""
            if isinstance(volume, str):
                parts = volume.split(":", 2)
                if len(parts) >= 2:
                    source, target = parts[0], parts[1]
            elif isinstance(volume, dict) and volume.get("type", "volume") == "bind":
                source = str(volume.get("source", ""))
                target = str(volume.get("target", ""))
            if forbidden_socket in {source, target}:
                raise PermissionError("安全红线：禁止挂载 Docker Socket")
            if source == "/":
                raise PermissionError("安全红线：禁止挂载宿主机根目录")
            if source.startswith("/") and not _is_under(source, allowed_bind_roots):
                raise PermissionError(f"绝对路径挂载未获允许：{source}")
    return document


def list_managed_servers() -> str:
    profiles = load_servers()
    if not profiles:
        return "尚未配置服务器。请检查 local/servers.yaml。"
    safe: list[dict[str, Any]] = []
    for name, profile in sorted(profiles.items()):
        safe.append(
            {
                "name": name,
                "host": profile.get("host"),
                "port": profile.get("port", 22),
                "username": profile.get("username"),
                "managed_root": profile.get("managed_root", "/srv/agenelf"),
                "allowed_operations": profile.get("allowed_operations", sorted(_OPERATION_RISKS)),
                "allowed_services": profile.get("allowed_services", []),
            }
        )
    return json.dumps(safe, ensure_ascii=False, indent=2)


def _change_message(request: dict[str, Any], target: str, operation: str, summary: str) -> str:
    first = (
        f"已复用相同未完成的运维请求：{request['id']}"
        if request.get("reused_existing")
        else f"运维请求已创建：{request['id']}"
    )
    return (
        f"{first}\n"
        f"风险级别：{request['risk']}\n"
        f"目标：{target}\n"
        f"操作：{operation}\n"
        f"摘要：{summary}\n"
        f"请求有效期至：{request.get('expires_at', '未知')}\n"
        + operations.approval_instructions(str(request["id"]))
        + "\n批准仅绑定本请求的目标、操作和参数；修改任何内容都必须重新申请。"
    )


def _submit(
    target: str,
    operation: str,
    parameters: dict[str, Any] | None,
    summary: str,
    wait_seconds: int = 0,
) -> str:
    profile = _profile(target)
    _allowed_operation(profile, operation)
    request = operations.submit_operation(
        capability="server.operations",
        operation=operation,
        target=target,
        parameters=parameters or {},
        risk=_OPERATION_RISKS[operation],
        summary=summary,
    )
    if request["risk"] == operations.RISK_READ:
        state = operations.wait_for_result(
            request["id"],
            timeout_seconds=max(0, min(int(wait_seconds), 8)),
        )
        return json.dumps(state, ensure_ascii=False, indent=2)
    return _change_message(request, target, operation, summary)


def inspect_server(target: str, wait_seconds: int = 3) -> str:
    return _submit(target, "inspect", {}, f"巡检服务器 {target}", wait_seconds)


def list_docker_containers(target: str, wait_seconds: int = 3) -> str:
    return _submit(target, "docker_ps", {}, f"查看 {target} 的 Docker 容器", wait_seconds)


def update_apt_index(target: str) -> str:
    return _submit(target, "apt_update", {}, f"在 {target} 执行 apt-get update")


def install_docker(target: str) -> str:
    return _submit(target, "docker_install", {}, f"在 {target} 安装并启动 Docker")


def deploy_compose_project(
    target: str,
    project: str,
    compose_yaml: str,
    pull: bool = True,
    plan_only: bool = False,
) -> str:
    profile = _profile(target)
    _allowed_operation(profile, "compose_deploy")
    project = str(project or "").strip()
    if not _PROJECT_RE.fullmatch(project):
        return "提交失败：project 只能包含字母、数字、点、下划线和短横线，最长 64 字符"
    try:
        document = validate_compose(compose_yaml, profile)
    except (ValueError, PermissionError) as exc:
        return f"Compose 安全校验失败：{exc}"
    services = sorted(str(item) for item in document["services"])
    preview = {
        "target": target,
        "project": project,
        "services": services,
        "managed_root": profile.get("managed_root", "/srv/agenelf"),
        "pull": bool(pull),
        "risk": operations.RISK_CHANGE,
    }
    if plan_only:
        return "Compose 计划校验通过：\n" + json.dumps(preview, ensure_ascii=False, indent=2)
    return _submit(
        target,
        "compose_deploy",
        {"project": project, "compose_yaml": compose_yaml, "pull": bool(pull)},
        f"部署 Compose 项目 {project}（服务：{', '.join(services)}）",
    )


def manage_system_service(
    target: str,
    service: str,
    action: str,
    wait_seconds: int = 3,
) -> str:
    try:
        profile = _profile(target)
        service = str(service or "").strip()
        _allowed_service(profile, service)
    except (ValueError, PermissionError) as exc:
        return f"操作失败：{exc}"
    action = str(action or "").strip().lower()
    if action == "status":
        return _submit(
            target,
            "service_status",
            {"service": service},
            f"查看 {target} 的 {service} 服务",
            wait_seconds,
        )
    if action == "restart":
        return _submit(
            target,
            "service_restart",
            {"service": service},
            f"重启 {target} 的 {service} 服务",
        )
    return "操作失败：action 只能是 status 或 restart"


def get_server_operation(operation_id: str, wait_seconds: int = 0) -> str:
    try:
        state = operations.wait_for_result(
            operation_id,
            timeout_seconds=max(0, min(int(wait_seconds), 8)),
        )
    except (TypeError, ValueError) as exc:
        return f"查询失败：{exc}"
    return json.dumps(state, ensure_ascii=False, indent=2)


# Compatibility helper, deliberately not exposed to the LLM. It permits only
# read-only local diagnostics; confirm/auth_id cannot authorize arbitrary shell.
def run_shell(command: str, confirm: bool = False, auth_id: str = "") -> str:
    del confirm, auth_id
    if permissions.classify_command(command) != "whitelist":
        return "已拒绝：通用 shell 执行已关闭；请使用结构化服务器运维工具。"
    try:
        argv = shlex.split(command)
        if not argv or not shutil.which(argv[0]):
            return f"执行失败：命令 {argv[0] if argv else ''!r} 不存在"
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        return f"执行失败：{exc}"
    return (
        f"退出码：{proc.returncode}\n"
        f"stdout:\n{proc.stdout or '（空）'}\n"
        f"stderr:\n{proc.stderr or '（空）'}"
    )


_DISPATCH = {
    "list_managed_servers": lambda args: list_managed_servers(),
    "inspect_server": lambda args: inspect_server(
        args.get("target", ""), args.get("wait_seconds", 3)
    ),
    "list_docker_containers": lambda args: list_docker_containers(
        args.get("target", ""), args.get("wait_seconds", 3)
    ),
    "update_apt_index": lambda args: update_apt_index(args.get("target", "")),
    "install_docker": lambda args: install_docker(args.get("target", "")),
    "deploy_compose_project": lambda args: deploy_compose_project(
        args.get("target", ""),
        args.get("project", ""),
        args.get("compose_yaml", ""),
        bool(args.get("pull", True)),
        bool(args.get("plan_only", False)),
    ),
    "manage_system_service": lambda args: manage_system_service(
        args.get("target", ""),
        args.get("service", ""),
        args.get("action", ""),
        args.get("wait_seconds", 3),
    ),
    "get_server_operation": lambda args: get_server_operation(
        args.get("operation_id", ""), args.get("wait_seconds", 0)
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
