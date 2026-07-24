from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.self_development import SelfDevelopmentEngine, SelfDevelopmentStore


class DummyRegistry:
    def __init__(self):
        self.skills = {
            "server_ops": object(),
            "self_reflection": object(),
            "self_development": object(),
        }
        self.errors = {}

    def capability_catalog(self):
        return [
            {
                "id": "server.operations",
                "name": "服务器运维",
                "domain": "operations",
                "version": "1",
                "operations": [],
                "composes_with": [],
            },
            {
                "id": "agent.self_reflection",
                "name": "自我反思",
                "domain": "agent-governance",
                "version": "1",
                "operations": [],
                "composes_with": [],
            },
            {
                "id": "agent.self_development",
                "name": "持续成长",
                "domain": "agent-governance",
                "version": "1",
                "operations": [],
                "composes_with": [],
            },
        ]

    def dispatch(self, tool_name, args):
        return f"unused:{tool_name}"


class DummyMemory:
    def __init__(self):
        self.episodes = 0
        self.memories = []

    def stats(self):
        return {
            "entries": len(self.memories),
            "max_entries": 100,
            "kinds": {"episode": self.episodes, "fact": 0, "preference": 0},
        }


class DummyLLM:
    model = "dummy"

    def chat(self, messages, tools=None):
        return {
            "content": json.dumps(
                {
                    "summary": "深度复盘完成",
                    "observations": ["测试覆盖仍可提升"],
                    "lessons": ["每个改动都要有回归测试"],
                    "intentions": [
                        {
                            "title": "提升测试覆盖",
                            "rationale": "降低回归风险",
                            "priority": "P2",
                            "acceptance_criteria": ["新增回归测试"],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            "tool_calls": [],
        }


class DummyAgent:
    def __init__(self, root: Path, *, threshold: int = 2):
        local = root / "local"
        (local / "memory").mkdir(parents=True)
        self.config = {
            "runtime_root": str(root),
            "local_dir": str(local),
            "self_dir": str(local / "self"),
            "agent": {"name": "TestElf"},
            "self_development": {
                "auto_reflect_every_episodes": threshold,
                "min_reflection_interval_seconds": 0,
                "max_reflections": 5,
                "max_intentions": 10,
                "allow_llm_reflection": True,
            },
        }
        self.registry = DummyRegistry()
        self.memory = DummyMemory()
        self.llm = DummyLLM()

    def local_status(self):
        return {"warnings": [], "fingerprint": "local-test"}


class SelfDevelopmentStoreTest(unittest.TestCase):
    def test_intention_is_deduplicated_redacted_and_persistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SelfDevelopmentStore(Path(tmp) / "self")
            first, created = store.create_intention(
                title="修复 token=super-secret-value 的问题",
                priority="P1",
                acceptance_criteria=["测试通过"],
            )
            self.assertTrue(created)
            self.assertIn("[REDACTED]", first["title"])
            second, created_again = store.create_intention(
                title="修复 token=super-secret-value 的问题",
                priority="P1",
            )
            self.assertFalse(created_again)
            self.assertEqual(first["id"], second["id"])
            reloaded = SelfDevelopmentStore(Path(tmp) / "self")
            self.assertEqual(reloaded.get_intention(first["id"])["status"], "proposed")
            self.assertFalse(reloaded.status()["operational_identity"]["consciousness_claim"])

    def test_reflections_are_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SelfDevelopmentStore(Path(tmp) / "self", max_reflections=2)
            for index in range(3):
                store.record_reflection(
                    trigger="test",
                    summary=f"reflection-{index}",
                    observations=[],
                    lessons=[],
                )
            values = store.recent_reflections(10)
            self.assertEqual(len(values), 2)
            self.assertEqual(values[0]["summary"], "reflection-2")


class SelfDevelopmentEngineTest(unittest.TestCase):
    def test_reflection_sediments_and_creates_intention(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = DummyAgent(root)
            engine = SelfDevelopmentEngine(agent, root=root)
            result = engine.reflect(
                trigger="manual",
                note="token=very-secret-token",
                deep=False,
            )
            reflection = result["reflection"]
            self.assertEqual(reflection["trigger"], "manual")
            self.assertIn("[REDACTED]", " ".join(reflection["observations"]))
            self.assertTrue(result["created_intention_ids"])
            self.assertTrue((root / "local" / "self" / "reflections.json").is_file())
            self.assertFalse(reflection["consciousness_claim"])

    def test_deep_reflection_is_structured_and_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = DummyAgent(root)
            result = SelfDevelopmentEngine(agent, root=root).reflect(deep=True)
            self.assertTrue(result["reflection"]["deep_reflection"])
            titles = [
                item["title"]
                for item in result["development"]["open_intentions"]
            ]
            self.assertIn("提升测试覆盖", titles)

    def test_auto_reflection_obeys_episode_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = DummyAgent(root, threshold=2)
            engine = SelfDevelopmentEngine(agent, root=root)
            agent.memory.episodes = 2
            first = engine.maybe_reflect()
            self.assertIsNotNone(first)
            second = engine.maybe_reflect()
            self.assertIsNone(second)
            self.assertEqual(engine.status()["episode_cursor"], 2)


    def test_awaiting_promotion_completes_only_with_host_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = DummyAgent(root)
            engine = SelfDevelopmentEngine(agent, root=root)
            created = engine.create_intention(
                title="完成可验证改进",
                priority="P1",
            )
            intention_id = created["intention"]["id"]
            engine.store.update_intention(
                intention_id,
                status="awaiting_promotion",
                evolution_session_id="evo-proof",
            )
            self.assertEqual(
                engine.get_intention(intention_id)["status"],
                "awaiting_promotion",
            )
            (root / "data" / "promotion-history" / "evo-proof").mkdir(
                parents=True
            )
            engine.reconcile()
            self.assertEqual(
                engine.get_intention(intention_id)["status"],
                "completed",
            )

    def test_intention_can_generate_plan_without_code_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent = DummyAgent(root)
            engine = SelfDevelopmentEngine(agent, root=root)
            created = engine.create_intention(
                title="改进错误诊断",
                rationale="让失败更容易定位",
                priority="P1",
            )
            intention_id = created["intention"]["id"]
            result = engine.pursue_intention(intention_id, apply_changes=False)
            self.assertEqual(result["cycle"]["status"], "plan_ready")
            self.assertEqual(result["intention"]["status"], "planned")
            cycle_path = (
                root
                / "data"
                / "autonomy-cycles"
                / f"{result['cycle']['id']}.json"
            )
            stored = json.loads(cycle_path.read_text(encoding="utf-8"))
            self.assertEqual(stored["source_intention_id"], intention_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
