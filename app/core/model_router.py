"""Provider-independent deterministic model routing.

The router selects an owner-configured model alias by task type, capabilities,
cost and privacy.  It never reads or returns credential values; external provider
profiles refer only to environment-variable names.  The router is a policy component,
not a model client, so every selected provider still uses the existing LLM adapter.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

TASK_TYPES = {"routine", "reasoning", "coding", "privacy", "vision", "voice"}
PROTOCOLS = {"openai_compatible", "ollama"}
COST_CLASSES = {"local": 0, "low": 1, "medium": 2, "high": 3}
PRIVACY_CLASSES = {"local", "external"}
_ALIAS_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_ENV_RE = re.compile(r"[A-Z][A-Z0-9_]{2,127}")
_SENSITIVE_KEYS = re.compile(r"(?:api[_-]?key|token|password|secret|credential)", re.I)


class ModelRouterError(ValueError):
    """Invalid model routing configuration or request."""


_DEFAULT_CONFIG: dict[str, Any] = {
    "providers": {
        "deepseek": {
            "enabled": True,
            "protocol": "openai_compatible",
            "model": "deepseek-v4-pro",
            "api_key_env": "DEEPSEEK_API_KEY",
            "capabilities": ["chat", "tools", "reasoning", "coding"],
            "cost_class": "low",
            "privacy": "external",
        },
        "ollama": {
            "enabled": False,
            "protocol": "ollama",
            "model": "qwen3-coder",
            "capabilities": ["chat", "coding", "privacy"],
            "cost_class": "local",
            "privacy": "local",
        },
    },
    "routes": {
        "routine": ["deepseek", "ollama"],
        "reasoning": ["deepseek"],
        "coding": ["deepseek", "ollama"],
        "privacy": ["ollama"],
        "vision": [],
        "voice": ["deepseek"],
    },
}


def _safe_text(value: object, limit: int = 500) -> str:
    text = " ".join(str(value or "").strip().split())
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


class ModelRouter:
    def __init__(self, config_path: str | Path | None = None):
        self.config_path = Path(config_path).resolve() if config_path else None
        self.providers: dict[str, dict[str, Any]] = {}
        self.routes: dict[str, list[str]] = {}
        self.warnings: list[str] = []
        self.reload()

    def _load_raw(self) -> dict[str, Any]:
        if self.config_path is None or not self.config_path.is_file():
            return _DEFAULT_CONFIG
        if self.config_path.is_symlink():
            raise ModelRouterError("models.yaml 不允许使用符号链接")
        try:
            value = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise ModelRouterError(f"无法读取模型路由配置：{exc}") from exc
        if not isinstance(value, dict):
            raise ModelRouterError("模型路由配置顶层必须是对象")
        return value

    @staticmethod
    def _reject_inline_secrets(profile: dict[str, Any], alias: str) -> None:
        for key, value in profile.items():
            if (
                _SENSITIVE_KEYS.search(str(key))
                and key != "api_key_env"
                and value not in {None, ""}
            ):
                raise ModelRouterError(
                    f"模型 {alias} 不得在 models.yaml 内保存凭据字段 {key!r}"
                )

    def reload(self) -> dict[str, Any]:
        raw = self._load_raw()
        raw_providers = raw.get("providers", {})
        raw_routes = raw.get("routes", {})
        if not isinstance(raw_providers, dict) or not isinstance(raw_routes, dict):
            raise ModelRouterError("providers 与 routes 必须是对象")
        providers: dict[str, dict[str, Any]] = {}
        warnings: list[str] = []
        for raw_alias, raw_profile in raw_providers.items():
            alias = str(raw_alias)
            if not _ALIAS_RE.fullmatch(alias):
                warnings.append(f"忽略非法模型别名：{alias!r}")
                continue
            if not isinstance(raw_profile, dict):
                warnings.append(f"忽略非对象模型配置：{alias}")
                continue
            self._reject_inline_secrets(raw_profile, alias)
            protocol = str(raw_profile.get("protocol", "openai_compatible"))
            if protocol not in PROTOCOLS:
                raise ModelRouterError(f"模型 {alias} protocol 非法：{protocol}")
            cost_class = str(raw_profile.get("cost_class", "medium"))
            if cost_class not in COST_CLASSES:
                raise ModelRouterError(f"模型 {alias} cost_class 非法：{cost_class}")
            privacy = str(raw_profile.get("privacy", "external"))
            if privacy not in PRIVACY_CLASSES:
                raise ModelRouterError(f"模型 {alias} privacy 非法：{privacy}")
            api_key_env = str(raw_profile.get("api_key_env", "")).strip()
            if protocol != "ollama" and api_key_env and not _ENV_RE.fullmatch(api_key_env):
                raise ModelRouterError(f"模型 {alias} api_key_env 非法")
            capabilities = raw_profile.get("capabilities", [])
            if not isinstance(capabilities, list):
                raise ModelRouterError(f"模型 {alias} capabilities 必须是数组")
            providers[alias] = {
                "alias": alias,
                "enabled": bool(raw_profile.get("enabled", True)),
                "protocol": protocol,
                "model": _safe_text(raw_profile.get("model"), 200),
                "api_key_env": api_key_env,
                "capabilities": sorted(
                    {
                        _safe_text(item, 100)
                        for item in capabilities
                        if _safe_text(item, 100)
                    }
                ),
                "cost_class": cost_class,
                "privacy": privacy,
                "base_url": _safe_text(raw_profile.get("base_url"), 500),
                "description": _safe_text(raw_profile.get("description"), 500),
            }
        routes: dict[str, list[str]] = {}
        for task_type in TASK_TYPES:
            values = raw_routes.get(task_type, [])
            if not isinstance(values, list):
                raise ModelRouterError(f"路由 {task_type} 必须是数组")
            ordered: list[str] = []
            for raw_alias in values:
                alias = str(raw_alias)
                if alias in providers and alias not in ordered:
                    ordered.append(alias)
            routes[task_type] = ordered
        self.providers = providers
        self.routes = routes
        self.warnings = warnings
        return self.catalog()

    @staticmethod
    def _ready(profile: dict[str, Any]) -> bool:
        if not profile.get("enabled"):
            return False
        if profile.get("protocol") == "ollama":
            return True
        env_name = str(profile.get("api_key_env", ""))
        return bool(env_name and os.environ.get(env_name))

    def catalog(self) -> dict[str, Any]:
        return {
            "config_present": bool(self.config_path and self.config_path.is_file()),
            "warnings": list(self.warnings),
            "providers": [
                {
                    "alias": alias,
                    "model": profile["model"],
                    "protocol": profile["protocol"],
                    "capabilities": list(profile["capabilities"]),
                    "cost_class": profile["cost_class"],
                    "privacy": profile["privacy"],
                    "enabled": profile["enabled"],
                    "ready": self._ready(profile),
                    "description": profile["description"],
                }
                for alias, profile in sorted(self.providers.items())
            ],
            "routes": {key: list(value) for key, value in sorted(self.routes.items())},
        }

    def route(
        self,
        task_type: str,
        *,
        required_capabilities: list[str] | None = None,
        prefer_local: bool = False,
        max_cost_class: str = "high",
    ) -> dict[str, Any]:
        task_type = str(task_type or "").strip()
        if task_type not in TASK_TYPES:
            raise ModelRouterError(f"未知任务类型：{task_type}")
        if max_cost_class not in COST_CLASSES:
            raise ModelRouterError(f"未知成本等级：{max_cost_class}")
        required = {
            _safe_text(item, 100)
            for item in (required_capabilities or [])
            if _safe_text(item, 100)
        }
        aliases = list(self.routes.get(task_type, []))
        if prefer_local:
            order = {alias: index for index, alias in enumerate(aliases)}
            aliases.sort(
                key=lambda alias: (
                    0 if self.providers[alias]["privacy"] == "local" else 1,
                    order[alias],
                )
            )
        candidates: list[dict[str, Any]] = []
        for alias in aliases:
            profile = self.providers[alias]
            if not profile["enabled"]:
                continue
            if COST_CLASSES[profile["cost_class"]] > COST_CLASSES[max_cost_class]:
                continue
            if not required.issubset(set(profile["capabilities"])):
                continue
            candidates.append(
                {
                    "alias": alias,
                    "model": profile["model"],
                    "protocol": profile["protocol"],
                    "cost_class": profile["cost_class"],
                    "privacy": profile["privacy"],
                    "ready": self._ready(profile),
                }
            )
        selected = next((item for item in candidates if item["ready"]), None)
        return {
            "task_type": task_type,
            "required_capabilities": sorted(required),
            "prefer_local": bool(prefer_local),
            "max_cost_class": max_cost_class,
            "selected": selected,
            "fallback_chain": candidates,
            "reason": (
                f"选择 {selected['alias']}：满足能力、成本、隐私和凭据就绪条件"
                if selected
                else "没有已就绪且满足约束的模型；不得静默降级到未配置提供商"
            ),
            "credentials_exposed": False,
        }
