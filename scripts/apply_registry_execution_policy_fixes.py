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
    '''        if action == "restart":
            return _contract(tool_name, "server.operations", "service_restart", "change", "queued_runner")
        return None
''',
    '''        if action == "restart":
            return _contract(tool_name, "server.operations", "service_restart", "change", "queued_runner")
        if not action:
            return ToolExecutionContract(
                tool_name, "server.operations", "dynamic:action", "read",
                "queued_runner", source="dynamic-placeholder"
            )
        return None
''',
)

replace_once(
    "app/core/agent.py",
    '''            policy_engine=PolicyEngine(config.get("policy_dir")),
''',
    '''            policy_engine=PolicyEngine(
                config.get("policy_dir")
                or str(
                    (Path(__file__).resolve().parents[2] / "policy").resolve()
                )
            ),
''',
)
replace_once(
    "app/core/agent.py",
    '''                result = self.registry.dispatch(
                    call["name"], call["arguments"], subject=subject
                )
''',
    '''                try:
                    result = self.registry.dispatch(
                        call["name"], call["arguments"], subject=subject
                    )
                except TypeError as exc:
                    # Compatibility for tests or extensions that monkeypatch the old
                    # two-argument dispatch signature. Real SkillRegistry supports subject.
                    if "unexpected keyword argument 'subject'" not in str(exc):
                        raise
                    result = self.registry.dispatch(call["name"], call["arguments"])
''',
)

replace_once(
    "app/core/configuration.py",
    '    config["policy_dir"] = str(root / "policy")\n',
    '''    runtime_policy_dir = root / "policy"
    source_policy_dir = app_dir.parent / "policy"
    config["policy_dir"] = str(
        runtime_policy_dir if runtime_policy_dir.is_dir() else source_policy_dir
    )
''',
)

replace_count(
    "docker-compose.yml",
    "      - ./scripts:/agenelf/scripts:ro\n",
    "      - ./scripts:/agenelf/scripts:ro\n      - ./policy:/agenelf/policy:ro\n",
    4,
)

print("registry execution policy compatibility fixes applied")
