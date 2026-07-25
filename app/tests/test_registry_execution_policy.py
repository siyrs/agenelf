from __future__ import annotations

import json
import os
import tempfile
import textwrap
import unittest
from pathlib import Path

from core.execution_policy import resolve_contract
from core.policy import PolicyEngine
from core.registry import SkillRegistry


ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"
POLICY = ROOT / "policy"


_READ_SKILL = '''
SKILL_META = {"name": "read_only_fake", "description": "read", "version": "1"}
CAPABILITY_META = {
    "id": "test.read",
    "operations": [{"name": "catalog", "description": "read", "risk": "read"}],
}
TOOLS = [{"type": "function", "function": {"name": "show_fake_catalog", "description": "x", "parameters": {"type": "object", "properties": {}, "required": []}}}]
def execute(tool_name, args):
    return "read-ok"
'''

_CHANGE_SKILL = '''
SKILL_META = {"name": "change_fake", "description": "change", "version": "1"}
CAPABILITY_META = {
    "id": "test.change",
    "operations": [{"name": "write_state", "description": "change", "risk": "change"}],
}
TOOLS = [{"type": "function", "function": {"name": "do_unclassified_write", "description": "x", "parameters": {"type": "object", "properties": {}, "required": []}}}]
def execute(tool_name, args):
    return "SHOULD-NOT-RUN"
'''


class RegistryExecutionPolicyTest(unittest.TestCase):
    def _registry(self, skills_dir: Path) -> SkillRegistry:
        registry = SkillRegistry(
            str(skills_dir),
            policy_engine=PolicyEngine(POLICY),
        )
        registry.discover()
        return registry

    def test_all_builtin_tools_are_classified(self):
        registry = self._registry(APP / "skills")
        self.assertEqual(registry.unclassified_tools(), [])
        catalog = registry.capability_catalog()
        self.assertTrue(catalog)
        self.assertTrue(all("tool_contracts" in item for item in catalog))

    def test_read_only_capability_can_inherit_pure_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "read_only_fake.py").write_text(
                textwrap.dedent(_READ_SKILL), encoding="utf-8"
            )
            registry = self._registry(directory)
            self.assertEqual(registry.dispatch("show_fake_catalog", {}), "read-ok")
            contract = registry.contract_for("show_fake_catalog")
            self.assertIsNotNone(contract)
            self.assertEqual(contract.execution_mode, "pure")
            self.assertEqual(contract.risk, "read")

    def test_unclassified_change_tool_is_denied_before_execute(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "change_fake.py").write_text(
                textwrap.dedent(_CHANGE_SKILL), encoding="utf-8"
            )
            registry = self._registry(directory)
            self.assertEqual(registry.unclassified_tools(), ["do_unclassified_write"])
            result = registry.dispatch("do_unclassified_write", {})
            self.assertIn("策略拒绝", result)
            self.assertNotIn("SHOULD-NOT-RUN", result)

    def test_forbidden_and_host_controlled_tools_are_blocked_for_agent(self):
        registry = self._registry(APP / "skills")
        self.assertIn("策略拒绝", registry.dispatch("run_python", {"code": "print(1)"}))
        self.assertIn(
            "策略拒绝",
            registry.dispatch(
                "forge_skill",
                {"name": "x", "description": "x", "source_code": "x=1"},
                subject="agent",
            ),
        )

    def test_dynamic_contracts_resolve_from_validated_arguments(self):
        registry = self._registry(APP / "skills")
        module = registry.skills["server_ops"]
        status = resolve_contract("manage_system_service", {"action": "status"}, module)
        restart = resolve_contract("manage_system_service", {"action": "restart"}, module)
        invalid = resolve_contract("manage_system_service", {"action": "disable"}, module)
        self.assertEqual((status.operation, status.risk), ("service_status", "read"))
        self.assertEqual((restart.operation, restart.risk), ("service_restart", "change"))
        self.assertIsNone(invalid)

        development = registry.skills["self_development"]
        plan = resolve_contract(
            "pursue_improvement_intention", {"apply_changes": False}, development
        )
        apply = resolve_contract(
            "pursue_improvement_intention", {"apply_changes": True}, development
        )
        self.assertEqual(plan.execution_mode, "local_state")
        self.assertEqual(apply.execution_mode, "controlled_sandbox")

    def test_dispatch_audit_never_contains_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_root = os.environ.get("AGENELF_ROOT")
            os.environ["AGENELF_ROOT"] = tmp
            try:
                directory = Path(tmp) / "skills"
                directory.mkdir()
                (directory / "read_only_fake.py").write_text(
                    textwrap.dedent(_READ_SKILL), encoding="utf-8"
                )
                registry = self._registry(directory)
                secret = "sk-THIS-MUST-NOT-BE-LOGGED"
                self.assertEqual(
                    registry.dispatch("show_fake_catalog", {"token": secret}),
                    "read-ok",
                )
                audit = Path(tmp) / "logs" / "policy-dispatch.jsonl"
                text = audit.read_text(encoding="utf-8")
                self.assertNotIn(secret, text)
                record = json.loads(text.splitlines()[-1])
                self.assertFalse(record["arguments_logged"])
                self.assertNotIn("token", record)
            finally:
                if old_root is None:
                    os.environ.pop("AGENELF_ROOT", None)
                else:
                    os.environ["AGENELF_ROOT"] = old_root


if __name__ == "__main__":
    unittest.main()
