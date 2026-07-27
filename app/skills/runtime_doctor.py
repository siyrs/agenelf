"""Read-only runtime doctor for runners, queues, mounts and skill loading."""
from __future__ import annotations

import json
from typing import Any

from core import runtime_health

SKILL_META = {
    "name": "runtime_doctor",
    "description": (
        "确定性检查 Agenelf 运行时代码来源、隔离 Runner 心跳、队列、挂载与技能加载错误，"
        "给出不包含凭据的修复建议。"
    ),
    "version": "1.0.0",
}

CAPABILITY_META = {
    "id": "agent.runtime_doctor",
    "name": "运行时体检",
    "description": "读取 Runner 心跳和本地运行状态，快速定位未启动、陈旧或挂载异常。",
    "version": "1.0.0",
    "domain": "operations",
    "operations": [
        {
            "name": "runtime_doctor",
            "description": "检查运行时、Runner、队列、挂载和技能注册状态",
            "risk": "read",
            "execution_mode": "pure",
        }
    ],
    "composes_with": [
        "server.operations",
        "docker.operations",
        "software.validation",
        "agent.authorized_self_upgrade",
        "agent.task_continuation",
    ],
}

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "runtime_doctor",
            "description": (
                "当 Runner 未响应、审批超时、升级停滞、运行时代码疑似陈旧或需要整体体检时调用。"
                "这是只读检查，不访问 SSH 私钥、审批密钥或主人秘密。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    }
]

_RUNTIME_AGENT: Any | None = None
_RUNTIME_CONFIG: dict[str, Any] = {}


def configure_runtime(
    *,
    agent: Any,
    config: dict[str, Any] | None = None,
    **_: Any,
) -> None:
    global _RUNTIME_AGENT, _RUNTIME_CONFIG
    _RUNTIME_AGENT = agent
    _RUNTIME_CONFIG = config if isinstance(config, dict) else getattr(agent, "config", {})


def runtime_doctor() -> dict[str, Any]:
    registry = getattr(_RUNTIME_AGENT, "registry", None) if _RUNTIME_AGENT is not None else None
    return runtime_health.diagnose(
        registry=registry,
        config=_RUNTIME_CONFIG,
    )


def execute(tool_name: str, args: dict[str, Any]) -> str:
    del args
    if tool_name != "runtime_doctor":
        return json.dumps({"status": "failed", "error": f"未知工具：{tool_name}"}, ensure_ascii=False)
    try:
        return json.dumps(runtime_doctor(), ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps(
            {"status": "failed", "error": f"{type(exc).__name__}: {exc}"},
            ensure_ascii=False,
            indent=2,
        )
