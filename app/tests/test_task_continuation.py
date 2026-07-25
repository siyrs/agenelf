from __future__ import annotations

import unittest

from skills import task_continuation


class FakeMemory:
    def __init__(self):
        self.entries: list[tuple[str, str]] = []

    def add(self, kind: str, content: str) -> None:
        self.entries.append((kind, content))


class FakeAgent:
    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []
        self.system_prompt = ""
        self.memory = FakeMemory()

    def _refresh_system_prompt(self) -> None:
        self.system_prompt = "base prompt"

    def chat(self, user_input: str, *, subject: str = "agent") -> str:
        self.calls.append((user_input, subject))
        return self.replies.pop(0)


class TaskContinuationRuntimeTest(unittest.TestCase):
    def test_max_round_sentinel_automatically_continues_original_goal(self):
        agent = FakeAgent(
            [task_continuation._MAX_ROUND_SENTINEL, "VPN 已完成诊断"]
        )
        task_continuation.configure_runtime(
            agent=agent,
            registry=None,
            config={"agent": {"continuation_segments": 3}},
        )

        result = agent.chat("修复 pve-ubuntu 的 sing-box", subject="cli")

        self.assertEqual(result, "VPN 已完成诊断")
        self.assertEqual(len(agent.calls), 2)
        self.assertEqual(agent.calls[0][0], "修复 pve-ubuntu 的 sing-box")
        self.assertIn("原始用户目标：修复 pve-ubuntu 的 sing-box", agent.calls[1][0])
        self.assertEqual(agent.calls[1][1], "cli")
        self.assertIn("任务连续性运行时约束", agent.system_prompt)
        self.assertIn("技能变更只是中间步骤", agent.system_prompt)

    def test_total_budget_exhaustion_returns_and_persists_recoverable_checkpoint(self):
        agent = FakeAgent(
            [
                task_continuation._MAX_ROUND_SENTINEL,
                task_continuation._MAX_ROUND_SENTINEL,
                task_continuation._MAX_ROUND_SENTINEL,
            ]
        )
        task_continuation.configure_runtime(
            agent=agent,
            registry=None,
            config={"agent": {"continuation_segments": 3}},
        )

        result = agent.chat("继续迭代 Docker 技能", subject="cli")

        self.assertIn("可恢复检查点", result)
        self.assertIn("已自动续办：3 个有界工具段", result)
        self.assertNotEqual(result, task_continuation._MAX_ROUND_SENTINEL)
        self.assertEqual(len(agent.calls), 3)
        self.assertEqual(len(agent.memory.entries), 1)
        self.assertIn("原始目标：继续迭代 Docker 技能", agent.memory.entries[0][1])

    def test_binding_is_idempotent(self):
        agent = FakeAgent(["完成"])
        task_continuation.configure_runtime(agent=agent, registry=None, config={})
        first_chat = agent.chat
        task_continuation.configure_runtime(agent=agent, registry=None, config={})
        second_chat = agent.chat

        self.assertIs(first_chat.__func__, second_chat.__func__)
        self.assertEqual(agent.chat("目标"), "完成")
        self.assertEqual(len(agent.calls), 1)

    def test_segment_budget_is_bounded(self):
        self.assertEqual(
            task_continuation._segment_budget(
                {"agent": {"continuation_segments": 99}}
            ),
            6,
        )
        self.assertEqual(
            task_continuation._segment_budget(
                {"agent": {"continuation_segments": 1}}
            ),
            2,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
