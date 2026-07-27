"""Agent 多会话历史（session_id 分桶）行为测试。

覆盖：
- 默认桶（session_id=None）与旧的单一 self.history 完全兼容；
- 两个 session 的历史互不可见（模型消息与桶内容均隔离）；
- 桶截断策略与旧 history_max_messages 一致；
- clear_history 只清空目标桶；
- get_history / session_ids 只读视图。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from core.agent import DEFAULT_SESSION_ID, Agent


class RecordingLLM:
    model = "recording"
    temperature = 0.0

    def __init__(self):
        self.calls: list[list[dict]] = []
        self.counter = 0

    def chat(self, messages, tools=None):
        self.calls.append([dict(item) for item in messages])
        self.counter += 1
        return {"content": f"reply-{self.counter}", "tool_calls": []}


class AgentSessionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.skills_dir = self.root / "empty-skills"
        self.skills_dir.mkdir()
        self.old_root = os.environ.get("AGENELF_ROOT")
        os.environ["AGENELF_ROOT"] = str(self.root)

    def tearDown(self) -> None:
        if self.old_root is None:
            os.environ.pop("AGENELF_ROOT", None)
        else:
            os.environ["AGENELF_ROOT"] = self.old_root
        self.tmp.cleanup()

    def _agent(self, history_max: int = 12) -> Agent:
        agent = Agent(
            {
                "mock": True,
                "runtime_root": str(self.root),
                "skills_dir": str(self.skills_dir),
                "memory_path": str(self.root / "memory.json"),
                "persona_path": str(self.root / "persona.yaml"),
                "agent": {"history_max_messages": history_max},
            }
        )
        agent._maybe_auto_reflect = lambda: None
        agent.llm = RecordingLLM()
        return agent

    def test_default_session_matches_legacy_single_history(self):
        agent = self._agent()
        agent.chat("你好")
        agent.chat("跟进")
        self.assertEqual(
            [entry["role"] for entry in agent.history],
            ["user", "assistant", "user", "assistant"],
        )
        self.assertEqual(agent.history[0]["content"], "你好")
        self.assertEqual(agent.session_ids(), [DEFAULT_SESSION_ID])

    def test_sessions_are_isolated_from_each_other_and_default(self):
        agent = self._agent()
        agent.chat("默认桶消息")
        agent.chat("甲-1", session_id="alpha")
        agent.chat("甲-2", session_id="alpha")
        agent.chat("乙-1", session_id="beta")

        alpha = agent.get_history(session_id="alpha")
        beta = agent.get_history(session_id="beta")
        default = agent.get_history()

        self.assertEqual(
            [e["content"] for e in alpha if e["role"] == "user"], ["甲-1", "甲-2"]
        )
        self.assertEqual(
            [e["content"] for e in beta if e["role"] == "user"], ["乙-1"]
        )
        self.assertEqual(
            [e["content"] for e in default if e["role"] == "user"], ["默认桶消息"]
        )
        # 模型视角同样隔离：beta 调用的对话消息（不含共享系统提示词中的
        # 长期记忆块）不应看到 alpha / 默认桶的内容
        beta_call = agent.llm.calls[-1]
        conversation = [
            item for item in beta_call if item.get("role") != "system"
        ]
        rendered = str(conversation)
        self.assertIn("乙-1", rendered)
        self.assertNotIn("甲-1", rendered)
        self.assertNotIn("默认桶消息", rendered)

    def test_follow_up_within_session_sees_only_own_bucket(self):
        agent = self._agent()
        agent.chat("巡检 primary", session_id="ops")
        agent.chat("那 Docker 呢？", session_id="ops")
        second = agent.llm.calls[1]
        self.assertEqual(second[-3]["content"], "巡检 primary")
        self.assertEqual(second[-1]["content"], "那 Docker 呢？")

    def test_bucket_truncation_matches_legacy_strategy(self):
        agent = self._agent(history_max=4)
        for index in range(4):
            agent.chat(f"m{index}", session_id="s")
        bucket = agent.get_history(session_id="s")
        self.assertLessEqual(len(bucket), 4)
        # 截断后不得以 assistant 消息开头（保持 user/assistant 配对）
        self.assertEqual(bucket[0]["role"], "user")
        self.assertEqual(bucket[-1]["content"], "reply-4")

    def test_clear_history_only_clears_target_bucket(self):
        agent = self._agent()
        agent.chat("a1", session_id="alpha")
        agent.chat("b1", session_id="beta")
        agent.chat("d1")

        cleared = agent.clear_history(session_id="alpha")
        self.assertEqual(cleared, 2)
        self.assertEqual(agent.get_history(session_id="alpha"), [])
        self.assertEqual(len(agent.get_history(session_id="beta")), 2)
        self.assertEqual(len(agent.get_history()), 2)

        cleared_default = agent.clear_history()
        self.assertEqual(cleared_default, 2)
        self.assertEqual(agent.get_history(), [])
        self.assertEqual(len(agent.get_history(session_id="beta")), 2)

    def test_blank_session_id_falls_back_to_default_bucket(self):
        agent = self._agent()
        agent.chat("hello", session_id="  ")
        self.assertEqual(len(agent.get_history()), 2)
        self.assertEqual(agent.session_ids(), [DEFAULT_SESSION_ID])


if __name__ == "__main__":
    unittest.main(verbosity=2)
