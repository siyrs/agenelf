#!/usr/bin/env python3
"""Initialize and migrate Agenelf owner-specific data into root/local/."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / "local"


def _copy_if_missing(source: Path, target: Path, actions: list[str]) -> None:
    if target.exists() or not source.is_file():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    actions.append(f"复制 {source.relative_to(ROOT)} -> {target.relative_to(ROOT)}")


def _copy_tree_files(source: Path, target: Path, actions: list[str]) -> None:
    if not source.is_dir():
        return
    target.mkdir(parents=True, exist_ok=True)
    for path in source.iterdir():
        if not path.is_file():
            continue
        destination = target / path.name
        if destination.exists():
            continue
        shutil.copy2(path, destination)
        actions.append(f"迁移 {path.relative_to(ROOT)} -> {destination.relative_to(ROOT)}")


def _write_json_if_missing(path: Path, value, actions: list[str]) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    actions.append(f"创建 {path.relative_to(ROOT)}")


def _ensure_runtime_directories(actions: list[str]) -> None:
    directories = (
        ROOT / "logs",
        ROOT / "workspace" / "scratch",
        ROOT / "app-space" / "skills",
        ROOT / "app-tmp",
        ROOT / "app-fork",
        ROOT / "data" / "auth-requests",
        ROOT / "data" / "auth-decisions",
        ROOT / "data" / "auth-consumed",
        ROOT / "data" / "ops-requests",
        ROOT / "data" / "ops-results",
        ROOT / "data" / "ops-locks",
        ROOT / "data" / "approval-commands",
        ROOT / "data" / "approval-results",
        ROOT / "data" / "approval-locks",
        ROOT / "data" / "validation-requests",
        ROOT / "data" / "validation-results",
        ROOT / "data" / "validation-locks",
        ROOT / "data" / "repair-requests",
        ROOT / "data" / "repair-results",
        ROOT / "data" / "repair-locks",
        ROOT / "data" / "authorized-upgrades",
        ROOT / "data" / "self-upgrade-requests",
        ROOT / "data" / "self-upgrade-results",
        ROOT / "data" / "self-upgrade-locks",
        ROOT / "data" / "self-upgrade-backups",
        ROOT / "data" / "runner-health",
        ROOT / "data" / "tasks",
        ROOT / "data" / "channel-requests",
        ROOT / "data" / "promote-requests",
        ROOT / "data" / "promotion-history",
        ROOT / "data" / "autonomy-cycles",
    )
    for directory in directories:
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
            actions.append(f"创建 {directory.relative_to(ROOT)}/")


def initialize(migrate: bool = True) -> dict:
    actions: list[str] = []
    directories = (
        LOCAL,
        LOCAL / "context",
        LOCAL / "memory",
        LOCAL / "secrets",
        LOCAL / "self",
    )
    for directory in directories:
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
            actions.append(f"创建 {directory.relative_to(ROOT)}/")
    for directory in (ROOT / "code-workspaces", ROOT / "repair-space"):
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
            actions.append(f"创建 {directory.relative_to(ROOT)}/")
    _ensure_runtime_directories(actions)

    profile_sources = []
    servers_sources = []
    memory_sources = []
    if migrate:
        profile_sources.append(ROOT / "app" / "persona" / "persona.yaml")
        servers_sources.append(ROOT / "config" / "servers.yaml")
        memory_sources.append(ROOT / "app" / "memory_store" / "memory.json")
    profile_sources.append(LOCAL / "profile.example.yaml")
    servers_sources.append(LOCAL / "servers.example.yaml")

    for source in profile_sources:
        _copy_if_missing(source, LOCAL / "profile.yaml", actions)
    _copy_if_missing(
        LOCAL / "preferences.example.yaml", LOCAL / "preferences.yaml", actions
    )
    for source in servers_sources:
        _copy_if_missing(source, LOCAL / "servers.yaml", actions)
    _copy_if_missing(
        LOCAL / "validation.example.yaml", LOCAL / "validation.yaml", actions
    )
    _copy_if_missing(LOCAL / "models.example.yaml", LOCAL / "models.yaml", actions)
    _copy_if_missing(
        LOCAL / "repositories.example.yaml", LOCAL / "repositories.yaml", actions
    )

    memory_target = LOCAL / "memory" / "memory.json"
    for source in memory_sources:
        if source.is_file():
            _copy_if_missing(source, memory_target, actions)
    _write_json_if_missing(memory_target, [], actions)

    self_dir = LOCAL / "self"
    _write_json_if_missing(self_dir / "reflections.json", [], actions)
    _write_json_if_missing(self_dir / "intentions.json", [], actions)
    _write_json_if_missing(self_dir / "state.json", {}, actions)

    context_example = LOCAL / "context.example.md"
    context_target = LOCAL / "context" / "owner-notes.md"
    _copy_if_missing(context_example, context_target, actions)

    if migrate:
        _copy_tree_files(ROOT / "secrets", LOCAL / "secrets", actions)

    # Best-effort permissions; Windows may ignore chmod semantics.
    for directory in (
        LOCAL,
        LOCAL / "secrets",
        LOCAL / "memory",
        LOCAL / "self",
    ):
        try:
            directory.chmod(0o700)
        except OSError:
            pass
    files = (
        LOCAL / "profile.yaml",
        LOCAL / "preferences.yaml",
        LOCAL / "servers.yaml",
        LOCAL / "validation.yaml",
        LOCAL / "models.yaml",
        LOCAL / "repositories.yaml",
        memory_target,
        self_dir / "reflections.json",
        self_dir / "intentions.json",
        self_dir / "state.json",
    )
    for path in files:
        if path.exists():
            try:
                path.chmod(0o600)
            except OSError:
                pass
    for path in (LOCAL / "secrets").glob("*"):
        if path.is_file():
            try:
                path.chmod(0o600)
            except OSError:
                pass

    result = status()
    result["actions"] = actions
    return result


def status() -> dict:
    self_dir = LOCAL / "self"
    return {
        "root": str(ROOT),
        "local_dir": str(LOCAL),
        "profile": (LOCAL / "profile.yaml").is_file(),
        "preferences": (LOCAL / "preferences.yaml").is_file(),
        "servers": (LOCAL / "servers.yaml").is_file(),
        "validation": (LOCAL / "validation.yaml").is_file(),
        "models": (LOCAL / "models.yaml").is_file(),
        "repositories": (LOCAL / "repositories.yaml").is_file(),
        "code_workspaces": (ROOT / "code-workspaces").is_dir(),
        "repair_space": (ROOT / "repair-space").is_dir(),
        "context_dir": (LOCAL / "context").is_dir(),
        "memory": (LOCAL / "memory" / "memory.json").is_file(),
        "self_dir": self_dir.is_dir(),
        "self_state": (self_dir / "state.json").is_file(),
        "self_reflections": (self_dir / "reflections.json").is_file(),
        "self_intentions": (self_dir / "intentions.json").is_file(),
        "secrets_dir": (LOCAL / "secrets").is_dir(),
        "secret_file_count": sum(
            1 for path in (LOCAL / "secrets").glob("*") if path.is_file()
        )
        if (LOCAL / "secrets").is_dir()
        else 0,
        "approval_queue": (ROOT / "data" / "approval-commands").is_dir(),
        "approval_results": (ROOT / "data" / "approval-results").is_dir(),
        "authorized_upgrades": (ROOT / "data" / "authorized-upgrades").is_dir(),
        "self_upgrade_queue": (ROOT / "data" / "self-upgrade-requests").is_dir(),
        "self_upgrade_results": (ROOT / "data" / "self-upgrade-results").is_dir(),
        "self_upgrade_backups": (ROOT / "data" / "self-upgrade-backups").is_dir(),
        "runner_health": (ROOT / "data" / "runner-health").is_dir(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="初始化 Agenelf local 个性化目录")
    parser.add_argument("--status", action="store_true", help="只检查，不创建或迁移")
    parser.add_argument(
        "--no-migrate", action="store_true", help="不迁移旧 persona/config/secrets/memory"
    )
    args = parser.parse_args()
    result = status() if args.status else initialize(migrate=not args.no_migrate)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
