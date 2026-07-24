from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.agent import Agent


class AgentAutoReflectionTest(unittest.TestCase):
    def test_chat_triggers_bounded_operational_sedimentation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            local = root / "local"
            (local / "context").mkdir(parents=True)
            (local / "memory").mkdir()
            (local / "self").mkdir()
            (local / "profile.yaml").write_text(
                "owner: {name: TestOwner}\n", encoding="utf-8"
            )
            (local / "preferences.yaml").write_text("{}\n", encoding="utf-8")
            (local / "servers.yaml").write_text("servers: {}\n", encoding="utf-8")
            skills = root / "skills"
            skills.mkdir()
            agent = Agent(
                {
                    "mock": True,
                    "runtime_root": str(root),
                    "local_dir": str(local),
                    "self_dir": str(local / "self"),
                    "local_profile_path": str(local / "profile.yaml"),
                    "local_preferences_path": str(local / "preferences.yaml"),
                    "local_context_dir": str(local / "context"),
                    "servers_path": str(local / "servers.yaml"),
                    "memory_path": str(local / "memory" / "memory.json"),
                    "skills_dir": str(skills),
                    "agent": {
                        "name": "TestElf",
                        "max_tool_rounds": 2,
                        "history_max_messages": 4,
                        "memory_max_entries": 20,
                    },
                    "self_development": {
                        "auto_reflect_every_episodes": 1,
                        "min_reflection_interval_seconds": 0,
                        "max_reflections": 5,
                        "max_intentions": 10,
                        "allow_llm_reflection": False,
                    },
                }
            )
            reply = agent.chat("你好")
            self.assertTrue(reply)
            status = agent.self_development_status()
            self.assertEqual(status["reflection_count"], 1)
            self.assertEqual(status["episode_cursor"], 1)
            self.assertIsNotNone(agent.last_auto_reflection)
            prompt = agent.system_prompt
            self.assertIn("持续成长状态", prompt)
            self.assertIn("不是情感", prompt)
            self.assertFalse(
                status["operational_identity"]["consciousness_claim"]
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
