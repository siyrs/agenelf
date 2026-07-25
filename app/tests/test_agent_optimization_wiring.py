from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from core.agent import Agent

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class AgentOptimizationWiringTest(unittest.TestCase):
    """证明自我优化覆盖值在运行时真实生效（记忆参数与 LLM 温度）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.local = self.root / "local"
        for directory in (self.local / "context", self.local / "memory", self.local / "self"):
            directory.mkdir(parents=True)
        (self.local / "profile.yaml").write_text("owner: {name: Test}\n", encoding="utf-8")
        (self.local / "preferences.yaml").write_text("hobbies: [quality]\n", encoding="utf-8")
        (self.local / "servers.yaml").write_text("servers: {}\n", encoding="utf-8")
        (self.local / "validation.yaml").write_text("checks: {}\nsuites: {}\n", encoding="utf-8")
        self.old = {
            key: os.environ.get(key)
            for key in (
                "AGENELF_ROOT",
                "AGENELF_LOCAL_DIR",
                "AGENELF_VALIDATION_FILE",
                "AGENELF_SERVERS_FILE",
                "OPENAI_API_KEY",
            )
        }
        os.environ["AGENELF_ROOT"] = str(self.root)
        os.environ["AGENELF_LOCAL_DIR"] = str(self.local)
        os.environ["AGENELF_VALIDATION_FILE"] = str(self.local / "validation.yaml")
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ.pop("AGENELF_SERVERS_FILE", None)

    def tearDown(self):
        for key, value in self.old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def _config(self) -> dict:
        return {
            "mock": True,
            "runtime_root": str(self.root),
            "local_dir": str(self.local),
            "self_dir": str(self.local / "self"),
            "skills_dir": str(PROJECT_ROOT / "skills"),
            "memory_path": str(self.local / "memory" / "memory.json"),
            "local_profile_path": str(self.local / "profile.yaml"),
            "local_preferences_path": str(self.local / "preferences.yaml"),
            "local_context_dir": str(self.local / "context"),
            "servers_path": str(self.local / "servers.yaml"),
            "validation_path": str(self.local / "validation.yaml"),
            "agent": {"name": "Agenelf", "history_max_messages": 4},
            "self_development": {
                "auto_reflect_every_episodes": 999,
                "min_reflection_interval_seconds": 0,
            },
        }

    def test_memory_prompt_limit_override_reaches_new_agent_and_refresh(self):
        agent = Agent(self._config())
        self.assertEqual(agent.memory_prompt_limit, 50)
        applied, _ = agent.optimization.apply(
            "agent.memory_prompt_limit", 10, "验证运行期覆盖"
        )
        self.assertTrue(applied)
        # 同一实例：每轮刷新机制会重新读取覆盖值
        agent._refresh_system_prompt()
        self.assertEqual(agent.memory_prompt_limit, 10)
        # 新实例：构造时从持久化文件读取覆盖值
        fresh = Agent(self._config())
        self.assertEqual(fresh.memory_prompt_limit, 10)

    def test_temperature_override_is_set_on_llm_before_chat(self):
        agent = Agent(self._config())
        agent.chat("你好")
        self.assertAlmostEqual(agent.llm.temperature, 0.6)
        applied, _ = agent.optimization.apply("llm.temperature", 0.1, "验证温度覆盖")
        self.assertTrue(applied)
        agent.chat("再试一次")
        self.assertAlmostEqual(agent.llm.temperature, 0.1)

    def test_rejected_apply_never_reaches_runtime(self):
        agent = Agent(self._config())
        applied, _ = agent.optimization.apply(
            "agent.memory_prompt_limit", 5, "越界不应生效"
        )
        self.assertFalse(applied)
        agent._refresh_system_prompt()
        self.assertEqual(agent.memory_prompt_limit, 50)
        self.assertEqual(Agent(self._config()).memory_prompt_limit, 50)


if __name__ == "__main__":
    unittest.main(verbosity=2)
