"""Registry generation and scoped dispatch adapters for task continuation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import MethodType
from typing import Any

from . import continuation_scope as scope
from . import continuation_store as store


def skills_fingerprint(registry: Any) -> str:
    rows: list[dict[str, Any]] = []
    for name, module in sorted(getattr(registry, "skills", {}).items()):
        metadata = getattr(module, "SKILL_META", {})
        tools = [
            str(tool.get("function", {}).get("name", ""))
            for tool in getattr(module, "TOOLS", [])
            if isinstance(tool, dict)
        ]
        path = Path(str(getattr(module, "__file__", "")))
        file_sha = ""
        try:
            if path.is_file():
                file_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            pass
        rows.append(
            {
                "name": name,
                "version": (
                    str(metadata.get("version", ""))
                    if isinstance(metadata, dict)
                    else ""
                ),
                "origin": getattr(registry, "origin_of", lambda _: "")(name),
                "tools": tools,
                "file_sha256": file_sha,
            }
        )
    encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def refresh_generation(registry: Any, root: Path, reason: str) -> int:
    path = store.runtime_state_path(root)
    previous = store.read_json(path) or {}
    fingerprint = skills_fingerprint(registry)
    generation = int(previous.get("generation", 0) or 0)
    if previous.get("fingerprint") != fingerprint:
        generation += 1
    generation = max(1, generation)
    store.atomic_json(
        path,
        {
            "schema_version": 1,
            "generation": generation,
            "fingerprint": fingerprint,
            "skill_count": len(getattr(registry, "skills", {})),
            "reason": reason,
            "updated_at": store.now_iso(),
        },
    )
    registry.runtime_generation = generation
    registry.runtime_fingerprint = fingerprint
    return generation


def bind_generation(registry: Any, root: Path) -> None:
    if registry is None or getattr(
        registry, "_task_continuation_generation_bound", False
    ):
        return
    registry._task_continuation_generation_bound = True
    refresh_generation(registry, root, "startup_discovery")

    def wrap_method(name: str) -> None:
        current = getattr(registry, name, None)
        if not callable(current):
            return

        def wrapped(self: Any, *args: Any, **kwargs: Any):
            result = current(*args, **kwargs)
            succeeded = (
                bool(result[0])
                if isinstance(result, tuple) and result
                else bool(result)
            )
            if succeeded:
                refresh_generation(self, root, name)
            return result

        setattr(registry, name, MethodType(wrapped, registry))

    for method_name in (
        "reload",
        "register_new_skill",
        "register_external_skill",
        "unregister_external_skill",
    ):
        wrap_method(method_name)

    original_catalog = registry.capability_catalog

    def catalog_with_generation(self: Any) -> list[dict[str, Any]]:
        values = original_catalog()
        generation = int(getattr(self, "runtime_generation", 0) or 0)
        for item in values:
            if isinstance(item, dict):
                item["runtime_generation"] = generation
        return values

    registry.capability_catalog = MethodType(catalog_with_generation, registry)


def segment_budget(config: dict[str, Any] | None) -> int:
    raw = os.environ.get("AGENELF_CONTINUATION_SEGMENTS", "").strip()
    if not raw and isinstance(config, dict):
        agent_cfg = config.get("agent", {})
        if isinstance(agent_cfg, dict):
            raw = str(agent_cfg.get("continuation_segments", "")).strip()
    try:
        value = int(raw or "3")
    except ValueError:
        value = 3
    return max(2, min(value, 6))


def bind_fresh_model_context(agent: Any, registry: Any | None) -> None:
    """Make every model call observe skills loaded during the current turn."""

    llm = getattr(agent, "llm", None)
    current_chat = getattr(llm, "chat", None)
    if (
        llm is None
        or registry is None
        or not callable(current_chat)
        or getattr(llm, "_task_continuation_fresh_context_bound", False)
    ):
        return
    original_llm_chat = current_chat

    def chat_with_fresh_context(
        self: Any,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if (
            messages
            and isinstance(messages[0], dict)
            and messages[0].get("role") == "system"
        ):
            messages[0] = {**messages[0], "content": str(agent.system_prompt)}
        return original_llm_chat(
            messages, tools=registry.all_tool_schemas() or None
        )

    llm.chat = MethodType(chat_with_fresh_context, llm)
    llm._task_continuation_fresh_context_bound = True


def bind_dispatch_scope(registry: Any, root: Path) -> None:
    if registry is None or getattr(
        registry, "_task_continuation_scope_bound", False
    ):
        return
    registry._task_continuation_scope_bound = True
    original_dispatch = registry.dispatch

    def call_original(
        tool_name: str, args: dict[str, Any], subject: str
    ) -> str:
        try:
            return str(original_dispatch(tool_name, args, subject=subject))
        except TypeError as exc:
            if "unexpected keyword argument 'subject'" not in str(exc):
                raise
            return str(original_dispatch(tool_name, args))

    def dispatch_with_scope(
        self: Any,
        tool_name: str,
        args: dict[str, Any],
        *,
        subject: str = "agent",
    ) -> str:
        data = args if isinstance(args, dict) else {}
        checkpoint = store.active_checkpoint(root)
        candidate = scope.scope_candidate(self, tool_name, data)
        if checkpoint and candidate:
            existing = checkpoint.get("scope", {})
            if not isinstance(existing, dict):
                existing = {}
            conflict = scope.scope_conflict(existing, candidate)
            authorization = checkpoint.get("authorization", {})
            if (
                conflict
                and isinstance(authorization, dict)
                and authorization.get("granted")
            ):
                store.add_event(checkpoint, "scope_mismatch_blocked", conflict)
                checkpoint["status"] = "blocked"
                checkpoint["last_error"] = f"作用域变化：{conflict}"
                store.write_checkpoint(root, checkpoint)
                return (
                    "错误：当前工具目标超出主人已授权的 Docker 技能升级任务作用域；"
                    f"{conflict}。原授权不会跨服务器、SSH 身份或容器继承。"
                )
            checkpoint["scope"] = scope.merge_scope(existing, candidate)
            scope.save_last_scope(root, checkpoint["scope"])
            store.write_checkpoint(root, checkpoint)

        result = call_original(tool_name, data, subject)
        checkpoint = store.active_checkpoint(root)
        if checkpoint:
            fingerprint = scope.identity_from_result(result)
            if fingerprint:
                current_scope = checkpoint.get("scope", {})
                if not isinstance(current_scope, dict):
                    current_scope = {}
                old = str(
                    current_scope.get("ssh_identity_fingerprint", "") or ""
                )
                if old and old != fingerprint:
                    checkpoint["status"] = "blocked"
                    checkpoint["last_error"] = (
                        "SSH 身份指纹发生变化，原授权不继承"
                    )
                    store.add_event(
                        checkpoint,
                        "ssh_identity_changed",
                        f"old={old} new={fingerprint}",
                    )
                else:
                    current_scope["ssh_identity_fingerprint"] = fingerprint
                    checkpoint["scope"] = current_scope
                    scope.save_last_scope(root, current_scope)
                    store.add_event(
                        checkpoint, "ssh_identity_observed", fingerprint
                    )
            if tool_name == "evolution_request_promotion":
                scope.write_promotion_authorization(root, checkpoint, result)
            checkpoint["skill_generation_current"] = int(
                getattr(self, "runtime_generation", 0) or 0
            )
            store.write_checkpoint(root, checkpoint)
        return result

    registry.dispatch = MethodType(dispatch_with_scope, registry)
