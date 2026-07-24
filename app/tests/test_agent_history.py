from __future__ import annotations

import os
import tempfile
import unittest

from core.agent import Agent


class RecordingLLM:
    model = "recording"

    def __init__(self):
        self.calls = []
        self.counter = 0

    def chat(self, messages, tools=None):
        self.calls.append(messages)
        self.counter += 1
        return {"content": f"reply-{self.counter}", "tool_calls": []}


class AgentHistoryTest(unittest.TestCase):
    def test_follow_up_turn_contains_recent_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            skills = os.path.join(tmp, "skills")
            os.makedirs(skills)
            agent = Agent(
                {
                    "mock": True,
                    "skills_dir": skills,
                    "memory_path": os.path.join(tmp, "memory.json"),
                    "persona_path": os.path.join(tmp, "persona.yaml"),
                    "agent": {"history_max_messages": 4, "max_tool_rounds": 2},
                }
            )
            llm = RecordingLLM()
            agent.llm = llm
            self.assertEqual(agent.chat("巡检 primary"), "reply-1")
            self.assertEqual(agent.chat("那 Docker 呢？"), "reply-2")
            second = llm.calls[1]
            self.assertEqual(second[-3], {"role": "user", "content": "巡检 primary"})
            self.assertEqual(second[-2], {"role": "assistant", "content": "reply-1"})
            self.assertEqual(second[-1], {"role": "user", "content": "那 Docker 呢？"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
