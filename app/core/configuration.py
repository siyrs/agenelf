"""Shared configuration loader for CLI, API and runtime skills."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


def runtime_root(app_dir: str | Path) -> Path:
    configured = os.environ.get("AGENELF_ROOT", "").strip()
    return Path(configured).resolve() if configured else Path(app_dir).resolve().parent


def resolve_local_dir(root: str | Path) -> Path:
    configured = os.environ.get("AGENELF_LOCAL_DIR", "").strip()
    return Path(configured).resolve() if configured else Path(root).resolve() / "local"


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def load_config(
    *,
    app_dir: str | Path,
    config_path: str | Path | None = None,
) -> dict[str, Any]:
    """Load common config and anchor every mutable personalized path in local/."""

    app_dir = Path(app_dir).resolve()
    root = runtime_root(app_dir)
    local_dir = resolve_local_dir(root)
    path = Path(config_path).resolve() if config_path else app_dir / "config.yaml"
    config = _read_yaml(path)

    llm = config.setdefault("llm", {})
    if not isinstance(llm, dict):
        llm = {}
        config["llm"] = llm
    if os.environ.get("OPENAI_API_KEY"):
        llm["api_key"] = os.environ["OPENAI_API_KEY"]
    if os.environ.get("OPENAI_BASE_URL"):
        llm["base_url"] = os.environ["OPENAI_BASE_URL"]
    if os.environ.get("AGENELF_MODEL"):
        llm["model"] = os.environ["AGENELF_MODEL"]
    if os.environ.get("AGENELF_MOCK") == "1":
        config["mock"] = True

    profile_path = local_dir / "profile.yaml"
    legacy_persona = app_dir / "persona" / "persona.yaml"
    preferences_path = local_dir / "preferences.yaml"
    context_dir = local_dir / "context"
    memory_path = local_dir / "memory" / "memory.json"
    self_dir_override = os.environ.get("AGENELF_SELF_DIR", "").strip()
    self_dir = (
        Path(self_dir_override).resolve()
        if self_dir_override
        else local_dir / "self"
    )
    local_servers = local_dir / "servers.yaml"
    legacy_servers = root / "config" / "servers.yaml"
    servers_override = os.environ.get("AGENELF_SERVERS_FILE", "").strip()
    servers_path = (
        Path(servers_override).resolve()
        if servers_override
        else (local_servers if local_servers.is_file() else legacy_servers)
    )

    config["runtime_root"] = str(root)
    config["local_dir"] = str(local_dir)
    config["self_dir"] = str(self_dir)
    config.setdefault("skills_dir", str(app_dir / "skills"))
    config.setdefault(
        "persona_path", str(profile_path if profile_path.is_file() else legacy_persona)
    )
    config.setdefault("local_profile_path", str(profile_path))
    config.setdefault("local_preferences_path", str(preferences_path))
    config.setdefault("local_context_dir", str(context_dir))
    config.setdefault("memory_path", str(memory_path))
    config.setdefault("servers_path", str(servers_path))

    os.environ["AGENELF_LOCAL_DIR"] = str(local_dir)
    os.environ["AGENELF_SELF_DIR"] = str(self_dir)
    os.environ["AGENELF_SERVERS_FILE"] = str(servers_path)
    return config
