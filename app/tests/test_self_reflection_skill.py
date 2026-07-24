"""Self-reflection skill protocol and runtime binding tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from skills import self_reflection


class FakeRegistry:
    errors = {}
    skills = {"self_reflection": object(), "server_ops": object()}

    @staticmethod
    def capability_catalog():
        return [
            {"id": "agent.self_reflection", "domain": "agent-governance", "operations": []},
            {"id": "server.operations", "domain": "infrastructure", "operations": []},
        ]


class FakeLLM:
    model = "fake"


class FakeAgent:
    def __init__(self, root: Path):
        self.registry = FakeRegistry()
        self.llm = FakeLLM()
        self.config = {"agent": {"name": "Agenelf"}, "skills_dir": str(root / "skills")}


class SelfReflectionSkillTest(unittest.TestCase):
    def test_capability_contract(self):
        self.assertEqual(self_reflection.CAPABILITY_META["id"], "agent.self_reflection")
        risks = {item["name"]: item["risk"] for item in self_reflection.CAPABILITY_META["operations"]}
        self.assertEqual(risks["self_snapshot"], "read")
        self.assertEqual(risks["autonomy_cycle"], "change")

    def test_snapshot_after_runtime_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "skills").mkdir()
            self_reflection.configure_runtime(agent=FakeAgent(root))
            data = json.loads(self_reflection.execute("self_snapshot", {}))
            self.assertFalse(data["identity"]["consciousness_claim"])

    def test_unknown_tool(self):
        result = self_reflection.execute("missing", {})
        self.assertIn("未知工具", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
