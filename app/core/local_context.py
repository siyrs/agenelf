"""Owner-specific context loaded from the root ``local/`` directory.

Only explicitly safe files are read by the LLM-facing Agent. Server secrets are
never scanned: the Agent gets only profile/preferences/context notes plus a
redacted server-alias summary. SSH credentials remain runner-only.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from .privacy import redact_sensitive_text, sanitize_value

_ALLOWED_CONTEXT_SUFFIXES = {".md", ".txt", ".yaml", ".yml", ".json"}


class LocalContextStore:
    def __init__(
        self,
        local_dir: str | Path,
        *,
        profile_path: str | Path | None = None,
        preferences_path: str | Path | None = None,
        context_dir: str | Path | None = None,
        servers_path: str | Path | None = None,
        prompt_max_chars: int = 12_000,
        file_max_chars: int = 20_000,
        max_context_files: int = 20,
    ):
        self.local_dir = Path(local_dir).resolve()
        self.profile_path = Path(profile_path or self.local_dir / "profile.yaml").resolve()
        self.preferences_path = Path(
            preferences_path or self.local_dir / "preferences.yaml"
        ).resolve()
        self.context_dir = Path(context_dir or self.local_dir / "context").resolve()
        self.servers_path = Path(servers_path or self.local_dir / "servers.yaml").resolve()
        self.prompt_max_chars = max(0, int(prompt_max_chars))
        self.file_max_chars = max(1, int(file_max_chars))
        self.max_context_files = max(0, int(max_context_files))
        self.profile: dict[str, Any] = {}
        self.preferences: dict[str, Any] = {}
        self.context_files: dict[str, str] = {}
        self.server_summary: dict[str, Any] = {}
        self.warnings: list[str] = []
        self.fingerprint = ""
        self.reload()

    def _safe_read_text(self, path: Path, label: str) -> str | None:
        if not path.is_file():
            return None
        if path.is_symlink():
            self.warnings.append(f"{label}: 符号链接被拒绝")
            return None
        try:
            size = path.stat().st_size
            if size > self.file_max_chars * 4:
                self.warnings.append(f"{label}: 文件过大，已跳过")
                return None
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            self.warnings.append(f"{label}: 读取失败：{exc}")
            return None
        if len(text) > self.file_max_chars:
            self.warnings.append(f"{label}: 内容超过上限，已截断")
            text = text[: self.file_max_chars]
        return text

    def _load_mapping(self, path: Path, label: str) -> dict[str, Any]:
        text = self._safe_read_text(path, label)
        if text is None:
            return {}
        try:
            data = yaml.safe_load(text) or {}
        except yaml.YAMLError as exc:
            self.warnings.append(f"{label}: YAML 解析失败：{exc}")
            return {}
        if not isinstance(data, dict):
            self.warnings.append(f"{label}: 顶层必须是对象")
            return {}
        sanitized = sanitize_value(data, path=label, warnings=self.warnings)
        return sanitized if isinstance(sanitized, dict) else {}

    def _load_context_files(self) -> dict[str, str]:
        if not self.context_dir.is_dir():
            return {}
        result: dict[str, str] = {}
        for path in sorted(self.context_dir.rglob("*")):
            if len(result) >= self.max_context_files:
                self.warnings.append("context: 文件数量达到上限，其余文件未加载")
                break
            if (
                not path.is_file()
                or path.is_symlink()
                or path.suffix.lower() not in _ALLOWED_CONTEXT_SUFFIXES
            ):
                continue
            try:
                relative = path.resolve().relative_to(self.context_dir).as_posix()
            except ValueError:
                self.warnings.append(f"context/{path.name}: 路径逃逸，已跳过")
                continue
            text = self._safe_read_text(path, f"context/{relative}")
            if text is None:
                continue
            if path.suffix.lower() in {".yaml", ".yml", ".json"}:
                try:
                    data = (
                        json.loads(text)
                        if path.suffix.lower() == ".json"
                        else yaml.safe_load(text)
                    )
                    sanitized = sanitize_value(
                        data, path=f"context/{relative}", warnings=self.warnings
                    )
                    text = yaml.safe_dump(
                        sanitized, allow_unicode=True, sort_keys=False
                    ).strip()
                except (json.JSONDecodeError, yaml.YAMLError) as exc:
                    self.warnings.append(f"context/{relative}: 解析失败：{exc}")
                    continue
            else:
                text = redact_sensitive_text(text)
            result[relative] = text
        return result

    def _load_server_summary(self) -> dict[str, Any]:
        text = self._safe_read_text(self.servers_path, "servers.yaml")
        if text is None:
            return {"aliases": [], "profiles": {}}
        try:
            data = yaml.safe_load(text) or {}
        except yaml.YAMLError as exc:
            self.warnings.append(f"servers.yaml: 解析失败：{exc}")
            return {"aliases": [], "profiles": {}}
        raw = data.get("servers", {}) if isinstance(data, dict) else {}
        if not isinstance(raw, dict):
            self.warnings.append("servers.yaml: servers 必须是对象")
            return {"aliases": [], "profiles": {}}
        profiles: dict[str, Any] = {}
        for name, profile in raw.items():
            if not isinstance(profile, dict):
                continue
            profiles[str(name)] = {
                "allowed_operations": [
                    str(item) for item in profile.get("allowed_operations", [])
                ]
                if isinstance(profile.get("allowed_operations", []), list)
                else [],
                "allowed_services": [
                    str(item) for item in profile.get("allowed_services", [])
                ]
                if isinstance(profile.get("allowed_services", []), list)
                else [],
            }
        return {"aliases": sorted(profiles), "profiles": profiles}

    def reload(self) -> dict[str, Any]:
        self.warnings = []
        self.profile = self._load_mapping(self.profile_path, "profile")
        self.preferences = self._load_mapping(self.preferences_path, "preferences")
        self.context_files = self._load_context_files()
        self.server_summary = self._load_server_summary()
        digest_source = json.dumps(
            {
                "profile": self.profile,
                "preferences": self.preferences,
                "context": self.context_files,
                "servers": self.server_summary,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        self.fingerprint = hashlib.sha256(digest_source).hexdigest()
        return self.status()

    def prompt_block(self) -> str:
        sections: list[str] = []
        if self.profile:
            sections.append(
                "【主人基本信息】\n"
                + yaml.safe_dump(self.profile, allow_unicode=True, sort_keys=False).strip()
            )
        if self.preferences:
            sections.append(
                "【主人偏好、爱好与兴趣】\n"
                + yaml.safe_dump(
                    self.preferences, allow_unicode=True, sort_keys=False
                ).strip()
            )
        aliases = self.server_summary.get("aliases", [])
        if aliases:
            sections.append("【可操作服务器别名】\n- " + "\n- ".join(aliases))
        for relative, content in self.context_files.items():
            sections.append(f"【主人补充资料：{relative}】\n{content}")
        if not sections:
            return "（local/ 尚未配置安全可读的个性化资料）"
        text = "\n\n".join(sections)
        if len(text) > self.prompt_max_chars:
            text = text[: max(0, self.prompt_max_chars - 1)] + "…"
        return text

    def status(self) -> dict[str, Any]:
        return {
            "local_dir": str(self.local_dir),
            "profile_loaded": bool(self.profile),
            "preferences_loaded": bool(self.preferences),
            "context_files": sorted(self.context_files),
            "server_aliases": list(self.server_summary.get("aliases", [])),
            "warnings": list(self.warnings),
            "fingerprint": self.fingerprint,
            "secrets_visible_to_agent": False,
        }
