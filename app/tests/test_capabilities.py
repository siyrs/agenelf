from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from core.registry import SkillRegistry


class CapabilityRegistryTest(unittest.TestCase):
    def test_manifest_is_exposed_for_planning(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ops.py"
            path.write_text(
                textwrap.dedent(
                    '''
                    SKILL_META = {"name": "ops", "description": "ops", "version": "1.0"}
                    CAPABILITY_META = {
                        "id": "server.operations",
                        "name": "服务器运维",
                        "domain": "operations",
                        "operations": [{"name": "inspect", "description": "巡检", "risk": "read"}],
                        "composes_with": ["software.validation"],
                    }
                    TOOLS = [{"type": "function", "function": {"name": "inspect", "description": "x", "parameters": {"type": "object", "properties": {}, "required": []}}}]
                    def execute(tool_name, args):
                        return "ok"
                    '''
                ),
                encoding="utf-8",
            )
            registry = SkillRegistry(tmp)
            registry.discover()
            catalog = registry.capability_catalog()
            self.assertEqual(catalog[0]["id"], "server.operations")
            self.assertEqual(catalog[0]["operations"][0]["risk"], "read")
            self.assertIn("software.validation", catalog[0]["composes_with"])

    def test_duplicate_tool_name_rejects_second_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = '''
SKILL_META = {"name": "%s", "description": "x", "version": "1"}
TOOLS = [{"type": "function", "function": {"name": "same_tool", "description": "x", "parameters": {"type": "object", "properties": {}, "required": []}}}]
def execute(tool_name, args): return "ok"
'''
            Path(tmp, "a.py").write_text(source % "a", encoding="utf-8")
            Path(tmp, "b.py").write_text(source % "b", encoding="utf-8")
            registry = SkillRegistry(tmp)
            registry.discover()
            self.assertEqual(list(registry.skills), ["a"])
            self.assertIn("b", registry.errors)
            self.assertIn("工具名冲突", registry.errors["b"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
