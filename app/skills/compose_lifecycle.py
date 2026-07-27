"""Safe remote Docker Compose lifecycle operations.

``down_compose_project`` targets only a named project below the server's managed root.
It never accepts arbitrary shell, never passes ``--volumes`` or ``--rmi``, and always
requires an owner decision bound to the exact server, project and timeout parameters.
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
    "name": "compose_lifecycle",
    "description": (
        "通过隔离 SSH Runner 对受管 Docker Compose 项目执行安全 down；"
        "默认保留卷、镜像、Compose 文件和备份，且必须精确审批。"
    ),
    "version": "1.0.0",
}

CAPABILITY_META = {
    "id": "docker.compose_lifecycle",
    "name": "Docker Compose 生命周期",
    "description": "停止并移除指定受管 Compose 项目的容器和项目网络，不删除卷或镜像。",
    "version": "1.0.0",
    "domain": "operations",
    "operations": [
        {
            "name": "down_compose_project",
            "description": "对指定受管项目执行 docker compose down",
            "risk": "change",
            "execution_mode": "queued_runner",
        }
    ],
    "composes_with": [
        "server.operations",
        "docker.operations",
        "software.validation",
        "agent.task_continuation",
    ],
}

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "down_compose_project",
            "description": (
                "在目标服务器 managed_root/<project> 中执行 docker compose down。"
                "只移除该项目容器和网络，明确保留 named volumes、镜像、compose.yaml 与备份；"
                "提交后需要主人精确批准。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "local/servers.yaml 中的服务器别名"},
                    "project": {"type": "string", "description": "受管 Compose 项目目录名"},
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 120,
                        "description": "容器停止超时，默认 30 秒",
                    },
                    "remove_orphans": {
                        "type": "boolean",
                        "description": "是否移除同项目孤儿容器，默认 true",
                    },
                    "plan_only": {
                        "type": "boolean",
                        "description": "只展示精确计划，不提交操作",
                    },
                },
                "required": ["target", "project"],
            },
        },
    }
]

_PROJECT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def _root() -> Path:
    configured = os.environ.get("AGENELF_ROOT", "").strip()
    return Path(configured).resolve() if configured else Path(__file__).resolve().parents[2]


def _servers_path() -> Path:
    configured = os.environ.get("AGENELF_SERVERS_FILE", "").strip()
    return Path(configured).resolve() if configured else _root() / "local" / "servers.yaml"


def _profile(target: str) -> dict[str, Any]:
    path = _servers_path()
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"服务器配置读取失败：{exc}") from exc
    servers = document.get("servers", {}) if isinstance(document, dict) else {}
    if not isinstance(servers, dict) or not isinstance(servers.get(target), dict):
        raise ValueError(f"未配置服务器 {target!r}")
    return servers[target]


def _allowed(profile: dict[str, Any]) -> None:
    allowed = profile.get("allowed_operations")
    if allowed is None:
        return
    values = {str(item) for item in allowed} if isinstance(allowed, list) else set()
    # Backward compatibility: an owner who already allowed compose_deploy may stop the
    # same managed project after an exact approval.  Volumes/images are still preserved.
    if not ({"compose_down", "compose_deploy"} & values):
        raise PermissionError("服务器策略未允许 compose_down（也未允许兼容的 compose_deploy）")


def down_compose_project(
    target: str,
    project: str,
    timeout_seconds: int = 30,
    remove_orphans: bool = True,
    plan_only: bool = False,
) -> str:
    target = str(target or "").strip()
    project = str(project or "").strip()
    if not _PROJECT_RE.fullmatch(project):
        return "提交失败：project 只能包含字母、数字、点、下划线和短横线，最长 64 字符"
    try:
        profile = _profile(target)
        _allowed(profile)
        timeout = max(0, min(int(timeout_seconds), 120))
    except (TypeError, ValueError, PermissionError) as exc:
        return f"提交失败：{exc}"

    plan = {
        "target": target,
        "project": project,
        "managed_project_dir": (
            str(profile.get("managed_root", "/srv/agenelf")).rstrip("/") + "/" + project
        ),
        "action": "docker compose down",
        "timeout_seconds": timeout,
        "remove_orphans": bool(remove_orphans),
        "preserve": ["named_volumes", "images", "compose.yaml", ".agenelf-backups"],
        "forbidden_flags": ["--volumes", "--rmi"],
        "risk": operations.RISK_CHANGE,
    }
    if plan_only:
        return "Compose down 计划校验通过：\n" + json.dumps(plan, ensure_ascii=False, indent=2)

    request = operations.submit_operation(
        capability="server.operations",
        operation="compose_down",
        target=target,
        parameters={
            "project": project,
            "timeout_seconds": timeout,
            "remove_orphans": bool(remove_orphans),
        },
        risk=operations.RISK_CHANGE,
        summary=f"停止 Compose 项目 {target}/{project}，保留卷和镜像",
    )
    return (
        f"Compose down 请求已创建：{request['id']}\n"
        f"目标：{target}/{project}\n"
        f"停止超时：{timeout}s；移除孤儿容器：{'是' if remove_orphans else '否'}\n"
        "保留：named volumes、镜像、compose.yaml 和备份；不会使用 --volumes/--rmi。\n"
        f"在当前 Agenelf CLI 输入：/approve {request['id']}\n"
        f"或输入：审批通过 {request['id']}\n"
        f"Windows 备用：.\\scripts\\approve.ps1 {request['id']} approve\n"
        "审批只绑定当前服务器、项目和参数；参数变化必须重新申请。"
    )


_DISPATCH = {
    "down_compose_project": lambda args: down_compose_project(
        args.get("target", ""),
        args.get("project", ""),
        args.get("timeout_seconds", 30),
        bool(args.get("remove_orphans", True)),
        bool(args.get("plan_only", False)),
    )
}


def execute(tool_name: str, args: dict[str, Any]) -> str:
    handler = _DISPATCH.get(tool_name)
    if handler is None:
        return f"未知工具：{tool_name}"
    try:
        return str(handler(args or {}))
    except Exception as exc:
        return f"执行失败：{type(exc).__name__}: {exc}"
