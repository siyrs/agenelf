"""Model-governance tools backed by owner-local routing configuration."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from core.model_router import ModelRouter, ModelRouterError

SKILL_META = {
    "name": "model_routing",
    "description": "按任务类型、能力、成本和隐私选择 DeepSeek/GPT/GLM/Ollama 等模型别名，不暴露凭据。",
    "version": "1.0.0",
}

CAPABILITY_META = {
    "id": "agent.model_routing",
    "name": "多模型治理与路由",
    "description": "把模型视为不可信可替换规划器，以确定性策略选择主人配置的提供商。",
    "version": "1.0.0",
    "domain": "model-governance",
    "operations": [
        {"name": "catalog", "description": "列出脱敏模型目录与就绪状态", "risk": "read"},
        {"name": "route", "description": "为任务计算模型与回退链", "risk": "read"},
    ],
    "composes_with": [
        "agent.workflow",
        "agent.self_development",
        "code.repair",
        "software.validation",
    ],
}

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_model_profiles",
            "description": "列出 local/models.yaml 的脱敏模型别名、能力、成本、隐私和就绪状态；绝不返回 API Key。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "route_model_task",
            "description": "为 routine/reasoning/coding/privacy/vision/voice 任务选择已就绪模型和回退链。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_type": {
                        "type": "string",
                        "enum": ["routine", "reasoning", "coding", "privacy", "vision", "voice"],
                    },
                    "required_capabilities": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "prefer_local": {"type": "boolean"},
                    "max_cost_class": {
                        "type": "string",
                        "enum": ["local", "low", "medium", "high"],
                    },
                },
                "required": ["task_type"],
            },
        },
    },
]


def _config_path() -> Path:
    explicit = os.environ.get("AGENELF_MODELS_FILE", "").strip()
    if explicit:
        return Path(explicit).resolve()
    local_dir = os.environ.get("AGENELF_LOCAL_DIR", "").strip()
    root = Path(os.environ.get("AGENELF_ROOT", Path(__file__).resolve().parents[2])).resolve()
    return (Path(local_dir).resolve() if local_dir else root / "local") / "models.yaml"


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def execute(tool_name: str, args: dict) -> str:
    data = args or {}
    try:
        router = ModelRouter(_config_path())
        if tool_name == "list_model_profiles":
            return _dump({"ok": True, **router.catalog()})
        if tool_name == "route_model_task":
            required = data.get("required_capabilities", [])
            result = router.route(
                str(data.get("task_type", "")),
                required_capabilities=required if isinstance(required, list) else [],
                prefer_local=bool(data.get("prefer_local", False)),
                max_cost_class=str(data.get("max_cost_class", "high")),
            )
            return _dump({"ok": True, **result})
        return _dump({"ok": False, "error": f"未知工具：{tool_name}"})
    except (ModelRouterError, TypeError, ValueError) as exc:
        return _dump({"ok": False, "error": str(exc), "credentials_exposed": False})
    except Exception as exc:
        return _dump(
            {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "credentials_exposed": False,
            }
        )
