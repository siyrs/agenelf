#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

ROOT = Path.cwd()


def replace_once(path: str, old: str, new: str) -> None:
    file = ROOT / path
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one match, found {count}")
    file.write_text(text.replace(old, new, 1), encoding="utf-8")
    print(f"[fixed] {path}")


def replace_count(path: str, old: str, new: str, expected: int) -> None:
    file = ROOT / path
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count != expected:
        raise RuntimeError(f"{path}: expected {expected} matches, found {count}")
    file.write_text(text.replace(old, new), encoding="utf-8")
    print(f"[fixed x{expected}] {path}")


replace_once(
    "app/core/execution_policy.py",
    '    "list_managed_servers": _contract("list_managed_servers", "server.operations", "catalog", "read", "pure"),\n',
    '    "list_managed_servers": _contract("list_managed_servers", "server.operations", "catalog", "read", "pure"),\n    "get_server_operation": _contract("get_server_operation", "server.operations", "get_result", "read", "pure"),\n',
)
replace_once(
    "app/core/execution_policy.py",
    '_LEGACY_PURE_TOOLS = {"ask_llm", "growth_pulse"}',
    '_LEGACY_PURE_TOOLS = {"ask_llm", "growth_pulse", "summarize"}',
)
replace_once(
    "app/core/execution_policy.py",
    '''        if action == "restart":\n            return _contract(tool_name, "server.operations", "service_restart", "change", "queued_runner")\n        return None\n''',
    '''        if action == "restart":\n            return _contract(tool_name, "server.operations", "service_restart", "change", "queued_runner")\n        if not action:\n            return ToolExecutionContract(\n                tool_name, "server.operations", "dynamic:action", "read",\n                "queued_runner", source="dynamic-placeholder"\n            )\n        return None\n''',
)

replace_once(
    "app/core/agent.py",
    '''                result = self.registry.dispatch(\n                    call["name"], call["arguments"], subject=subject\n                )\n''',
    '''                try:\n                    result = self.registry.dispatch(\n                        call["name"], call["arguments"], subject=subject\n                    )\n                except TypeError as exc:\n                    # Compatibility for tests or extensions that monkeypatch the old\n                    # two-argument dispatch signature. Real SkillRegistry supports subject.\n                    if "unexpected keyword argument 'subject'" not in str(exc):\n                        raise\n                    result = self.registry.dispatch(call["name"], call["arguments"])\n''',
)

replace_once(
    "app/core/configuration.py",
    '    config["policy_dir"] = str(root / "policy")\n',
    '''    runtime_policy_dir = root / "policy"\n    source_policy_dir = app_dir.parent / "policy"\n    config["policy_dir"] = str(\n        runtime_policy_dir if runtime_policy_dir.is_dir() else source_policy_dir\n    )\n''',
)

replace_count(
    "docker-compose.yml",
    "      - ./scripts:/agenelf/scripts:ro\n",
    "      - ./scripts:/agenelf/scripts:ro\n      - ./policy:/agenelf/policy:ro\n",
    4,
)

print("registry execution policy compatibility fixes applied")
