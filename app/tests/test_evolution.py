"""The legacy EvolutionEngine must route to the controlled autonomy pipeline."""

from __future__ import annotations

import unittest

from evolution.engine import EvolutionEngine


class FakeAgent:
    def __init__(self, status="promotion_requested"):
        self.status = status
        self.calls = []

    def run_autonomy_cycle(self, goal, apply_changes=False):
        self.calls.append((goal, apply_changes))
        return {"id": "auto-test", "goal": goal, "status": self.status}


class EvolutionAdapterTest(unittest.TestCase):
    def test_evolve_uses_sandbox_autonomy(self):
        agent = FakeAgent()
        result = EvolutionEngine(agent).evolve("完善自身")
        self.assertEqual(result["status"], "promotion_requested")
        self.assertEqual(agent.calls, [("完善自身", True)])

    def test_legacy_propose_does_not_use_git(self):
        agent = FakeAgent()
        ok, message = EvolutionEngine(agent).propose_core_change("安全升级", object())
        self.assertTrue(ok)
        self.assertIn("晋升请求", message)
        self.assertEqual(agent.calls, [("安全升级", True)])

    def test_failed_cycle_is_reported(self):
        agent = FakeAgent(status="failed")
        ok, message = EvolutionEngine(agent).propose_core_change("失败升级")
        self.assertFalse(ok)
        self.assertIn("failed", message)

    def test_requires_agent_contract(self):
        with self.assertRaises(TypeError):
            EvolutionEngine(object())


if __name__ == "__main__":
    unittest.main(verbosity=2)
