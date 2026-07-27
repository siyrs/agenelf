from __future__ import annotations

import json
import unittest

from skills import self_development


class FakeAgent:
    def self_development_status(self):
        return {"continuity_id": "self-test", "consciousness_claim": False}

    def reflect_and_sediment(self, note="", deep=False):
        return {"note": note, "deep": deep, "reflection": {"id": "reflection-test"}}

    def self_reflections(self, limit=10):
        return [{"id": "reflection-test"}][:limit]

    def improvement_intentions(self, status="", limit=20):
        return [{"id": "intent-test", "status": status or "proposed"}][:limit]

    def create_improvement_intention(
        self,
        *,
        title,
        rationale="",
        priority="P2",
        acceptance_criteria=None,
    ):
        return {
            "created": True,
            "intention": {
                "id": "intent-test",
                "title": title,
                "priority": priority,
                "acceptance_criteria": acceptance_criteria or [],
            },
        }

    def pursue_improvement_intention(self, intention_id, *, apply_changes=False):
        return {
            "intention": {"id": intention_id},
            "cycle": {"status": "promotion_requested" if apply_changes else "plan_ready"},
        }


class SelfDevelopmentSkillTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        self_development.configure_runtime(agent=FakeAgent())

    def test_capability_contract(self):
        self.assertEqual(
            self_development.CAPABILITY_META["id"], "agent.self_development"
        )
        risks = {
            item["name"]: item["risk"]
            for item in self_development.CAPABILITY_META["operations"]
        }
        self.assertEqual(risks["development_status"], "read")
        self.assertEqual(risks["pursue_intention"], "change")

    def test_tools_route(self):
        status = json.loads(
            self_development.execute("self_development_status", {})
        )
        self.assertEqual(status["continuity_id"], "self-test")
        reflection = json.loads(
            self_development.execute(
                "reflect_and_sediment", {"note": "复盘", "deep": True}
            )
        )
        self.assertTrue(reflection["deep"])
        intention = json.loads(
            self_development.execute(
                "create_improvement_intention",
                {"title": "补测试", "priority": "P1"},
            )
        )
        self.assertEqual(intention["intention"]["priority"], "P1")
        pursued = json.loads(
            self_development.execute(
                "pursue_improvement_intention",
                {"intention_id": "intent-test", "apply_changes": False},
            )
        )
        self.assertEqual(pursued["cycle"]["status"], "plan_ready")

    def test_unknown_tool_is_rejected(self):
        self.assertIn(
            "未知工具", self_development.execute("does_not_exist", {})
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
