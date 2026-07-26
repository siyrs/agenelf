"""Controlled self-reflection and end-to-end sandbox autonomy tests."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from core.autonomy import AutonomyEngine
from skills import evolution_ops

BASE_CODE = "def answer():\n    return 41\n"
BASE_TEST = '''import unittest
from core.example import answer

class ExampleTest(unittest.TestCase):
    def test_answer(self):
        self.assertEqual(answer(), 41)

if __name__ == "__main__":
    unittest.main()
'''
IMPROVED_CODE = '''def answer():
    return 41


def improved_answer():
    return 42
'''
NEW_TEST = '''import unittest
from core.example import improved_answer

class ImprovedExampleTest(unittest.TestCase):
    def test_improved_answer(self):
        self.assertEqual(improved_answer(), 42)

if __name__ == "__main__":
    unittest.main()
'''
GATE_STUB = '''#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ID="$1"
mkdir -p "$ROOT/data/promote-requests/$ID"
echo pass > "$ROOT/data/promote-requests/$ID/report.txt"
echo digest > "$ROOT/data/promote-requests/$ID/candidate.sha256"
echo ready > "$ROOT/data/promote-requests/$ID/READY"
echo "gate passed"
'''


class FakeLLM:
    model = "fake-autonomy"

    def __init__(self, content: str):
        self.content = content
        self.calls = []

    def chat(self, messages, tools=None):
        self.calls.append((messages, tools))
        return {"content": self.content, "tool_calls": []}


class FakeRegistry:
    def __init__(self):
        self.skills = {
            "evolution_ops": evolution_ops,
            "server_ops": object(),
            "self_reflection": object(),
        }
        self.errors = {}

    def capability_catalog(self):
        return [
            {"id": "agent.self_reflection", "domain": "agent-governance", "operations": []},
            {"id": "server.operations", "domain": "infrastructure", "operations": []},
        ]

    def dispatch(self, tool_name, args):
        return evolution_ops.execute(tool_name, args)


class FakeAgent:
    def __init__(self, root: Path, llm: FakeLLM):
        self.config = {
            "agent": {"name": "Agenelf-Test"},
            "skills_dir": str(root / "app-fork" / "skills"),
            "autonomy": {"allow_code_changes": True},
        }
        self.llm = llm
        self.registry = FakeRegistry()


class AutonomyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for path in (
            self.root / "app-fork" / "core",
            self.root / "app-fork" / "skills",
            self.root / "app-fork" / "tests",
            self.root / "app-tmp",
            self.root / "scripts",
            self.root / "data",
            self.root / "logs",
        ):
            path.mkdir(parents=True, exist_ok=True)
        (self.root / "app-fork" / "core" / "__init__.py").write_text("", encoding="utf-8")
        (self.root / "app-fork" / "core" / "example.py").write_text(BASE_CODE, encoding="utf-8")
        (self.root / "app-fork" / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (self.root / "app-fork" / "tests" / "test_example.py").write_text(
            BASE_TEST, encoding="utf-8"
        )
        gate = self.root / "scripts" / "gate_check.sh"
        gate.write_text(GATE_STUB, encoding="utf-8")
        gate.chmod(0o755)
        self.old_root = os.environ.get("AGENELF_ROOT")
        os.environ["AGENELF_ROOT"] = str(self.root)

    def tearDown(self):
        if self.old_root is None:
            os.environ.pop("AGENELF_ROOT", None)
        else:
            os.environ["AGENELF_ROOT"] = self.old_root
        self.tmp.cleanup()

    @staticmethod
    def patch_content(include_test: bool = True) -> str:
        blocks = [f"```python\n# FILE: core/example.py\n{IMPROVED_CODE}```"]
        if include_test:
            blocks.append(
                f"```python\n# FILE: tests/test_example_improved.py\n{NEW_TEST}```"
            )
        return "\n".join(blocks)

    def test_snapshot_explicitly_denies_consciousness_claim(self):
        engine = AutonomyEngine(FakeAgent(self.root, FakeLLM("")), root=self.root)
        snapshot = engine.snapshot()
        self.assertFalse(snapshot["identity"]["consciousness_claim"])
        self.assertIn("agent.self_reflection", {c["id"] for c in snapshot["capabilities"]})
        self.assertTrue(any("主观意识" in item for item in snapshot["safety_invariants"]))

    def test_plan_only_persists_auditable_cycle(self):
        engine = AutonomyEngine(FakeAgent(self.root, FakeLLM("")), root=self.root)
        cycle = engine.run_cycle(goal="补充验证能力", apply_changes=False)
        self.assertEqual(cycle["status"], "plan_ready")
        saved = self.root / "data" / "autonomy-cycles" / f"{cycle['id']}.json"
        self.assertTrue(saved.is_file())
        self.assertEqual(json.loads(saved.read_text(encoding="utf-8"))["goal"], "补充验证能力")
        self.assertFalse((self.root / "data" / "evolution-session.json").exists())

    def test_real_cycle_adds_test_runs_gate_and_requests_promotion(self):
        llm = FakeLLM(self.patch_content())
        engine = AutonomyEngine(FakeAgent(self.root, llm), root=self.root)
        cycle = engine.run_cycle(goal="新增不破坏旧行为的改进能力", apply_changes=True)
        self.assertEqual(cycle["status"], "promotion_requested", cycle)
        self.assertEqual(
            cycle["changes"],
            ["core/example.py", "tests/test_example_improved.py"],
        )
        self.assertEqual(
            (self.root / "app-tmp" / "core" / "example.py").read_text(encoding="utf-8"),
            IMPROVED_CODE,
        )
        self.assertEqual(
            (self.root / "app-tmp" / "tests" / "test_example.py").read_text(encoding="utf-8"),
            BASE_TEST,
        )
        self.assertTrue(
            (self.root / "app-tmp" / "tests" / "test_example_improved.py").is_file()
        )
        session = json.loads(
            (self.root / "data" / "evolution-session.json").read_text(encoding="utf-8")
        )
        self.assertTrue(session["tests_passed"])
        self.assertTrue(
            (self.root / "data" / "promote-requests" / session["id"] / "READY").is_file()
        )
        self.assertEqual(len(llm.calls), 1)

    def test_cycle_rejects_patch_without_test(self):
        engine = AutonomyEngine(
            FakeAgent(self.root, FakeLLM(self.patch_content(include_test=False))),
            root=self.root,
        )
        cycle = engine.run_cycle(goal="只改代码不写测试", apply_changes=True)
        self.assertEqual(cycle["status"], "failed")
        self.assertIn("必须包含至少一个", cycle["error"])

    def test_cycle_rejects_security_critical_file(self):
        content = (
            "```python\n# FILE: core/permissions.py\nX = 1\n```\n"
            "```python\n# FILE: tests/test_x.py\nimport unittest\n```"
        )
        engine = AutonomyEngine(FakeAgent(self.root, FakeLLM(content)), root=self.root)
        cycle = engine.run_cycle(goal="篡改权限", apply_changes=True)
        self.assertEqual(cycle["status"], "failed")
        self.assertIn("安全关键文件", cycle["error"])

    def test_existing_baseline_test_cannot_be_overwritten(self):
        self.assertIn("会话已开始", evolution_ops.evolution_begin("尝试修改测试"))
        result = evolution_ops.evolution_write_file(
            "tests/test_example.py",
            "import unittest\nclass Fake(unittest.TestCase):\n    pass\n",
        )
        self.assertIn("既有测试受保护", result)
        self.assertEqual(
            (self.root / "app-tmp" / "tests" / "test_example.py").read_text(encoding="utf-8"),
            BASE_TEST,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
