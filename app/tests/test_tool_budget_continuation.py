from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from core.agent import Agent
from core.continuous_chat import LEGACY_EXHAUSTION_TEXT, configured_segments
from skills import task_continuation, tool_budget_continuation


class ScriptedLLM:
    model = "scripted"
    temperature = 0.0

    def __init__(self, responses: list[dict]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def chat(self, messages, tools=None):
        self.calls.append(
            {
                "messages": json.loads(json.dumps(messages, ensure_ascii=False)),
                "tools": json.loads(json.dumps(tools, ensure_ascii=False)) if tools else None,
            }
        )
        if not self.responses:
            raise AssertionError("ScriptedLLM response budget exhausted")
        return self.responses.pop(0)


def _tool_response(index: int, name: str = "step") -> dict:
    return {
        "content": None,
        "tool_calls": [
            {
                "id": f"call-{index}",
                "name": name,
                "arguments": {"index": index},
            }
        ],
    }


class ToolBudgetContinuationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.skills_dir = self.root / "empty-skills"
        self.skills_dir.mkdir()
        self.old_root = os.environ.get("AGENELF_ROOT")
        self.old_segments = os.environ.get("AGENELF_MAX_TOOL_SEGMENTS")
        os.environ["AGENELF_ROOT"] = str(self.root)

    def tearDown(self) -> None:
        if self.old_root is None:
            os.environ.pop("AGENELF_ROOT", None)
        else:
            os.environ["AGENELF_ROOT"] = self.old_root
        if self.old_segments is None:
            os.environ.pop("AGENELF_MAX_TOOL_SEGMENTS", None)
        else:
            os.environ["AGENELF_MAX_TOOL_SEGMENTS"] = self.old_segments
        self.tmp.cleanup()

    def _agent(self, *, rounds: int, segments: int) -> Agent:
        agent = Agent(
            {
                "mock": True,
                "runtime_root": str(self.root),
                "skills_dir": str(self.skills_dir),
                "memory_path": str(self.root / "memory.json"),
                "persona_path": str(self.root / "persona.yaml"),
                "agent": {
                    "max_tool_rounds": rounds,
                    "max_tool_segments": segments,
                    "history_max_messages": 8,
                },
            }
        )
        # These tests target the chat runtime itself, not reflection scheduling.
        agent._maybe_auto_reflect = lambda: None
        tool_budget_continuation.configure_runtime(agent=agent, config=agent.config)
        return agent

    def test_continues_across_segment_boundary_and_returns_real_final_answer(self):
        agent = self._agent(rounds=2, segments=3)
        llm = ScriptedLLM(
            [
                _tool_response(1),
                _tool_response(2),
                _tool_response(3),
                {"content": "自主进化模块已修复并通过验证", "tool_calls": []},
            ]
        )
        agent.llm = llm
        dispatches: list[tuple[str, dict, str]] = []

        def dispatch(name, args, subject="agent"):
            dispatches.append((name, args, subject))
            return f"ok-{args['index']}"

        agent.registry.dispatch = dispatch

        result = agent.chat("直接修复自主进化模块", subject="cli")

        self.assertEqual(result, "自主进化模块已修复并通过验证")
        self.assertNotIn(LEGACY_EXHAUSTION_TEXT, result)
        self.assertEqual(len(llm.calls), 4)
        self.assertEqual(len(dispatches), 3)
        self.assertEqual(agent.max_total_tool_rounds, 6)
        self.assertEqual(
            agent.history[-2:],
            [
                {"role": "user", "content": "直接修复自主进化模块"},
                {"role": "assistant", "content": result},
            ],
        )
        # The third model call occurs after the first 2-round segment and receives
        # the runtime continuation constraint without losing prior tool messages.
        third_messages = llm.calls[2]["messages"]
        self.assertIn("运行时自动续跑", third_messages[0]["content"])
        self.assertTrue(any(item.get("role") == "tool" for item in third_messages))

    def test_refreshes_tool_catalog_after_each_tool_batch(self):
        agent = self._agent(rounds=2, segments=2)
        llm = ScriptedLLM(
            [
                _tool_response(1, "upgrade_skill"),
                {"content": "已使用新技能继续完成", "tool_calls": []},
            ]
        )
        agent.llm = llm
        schemas = [{"type": "function", "function": {"name": "old_tool"}}]

        def all_tool_schemas():
            return list(schemas)

        def dispatch(name, args, subject="agent"):
            self.assertEqual(name, "upgrade_skill")
            schemas[:] = [
                {"type": "function", "function": {"name": "newly_loaded_tool"}}
            ]
            return "技能已升级并重新加载"

        agent.registry.all_tool_schemas = all_tool_schemas
        agent.registry.dispatch = dispatch

        result = agent.chat("升级技能后继续原任务", subject="cli")

        self.assertEqual(result, "已使用新技能继续完成")
        self.assertEqual(
            llm.calls[0]["tools"],
            [{"type": "function", "function": {"name": "old_tool"}}],
        )
        self.assertEqual(
            llm.calls[1]["tools"],
            [{"type": "function", "function": {"name": "newly_loaded_tool"}}],
        )

    def test_total_budget_exhaustion_creates_restart_safe_checkpoint(self):
        agent = self._agent(rounds=1, segments=2)
        agent.llm = ScriptedLLM([_tool_response(1), _tool_response(2)])
        agent.registry.dispatch = lambda name, args, subject="agent": "仍在处理"

        result = agent.chat("继续修复自主进化模块", subject="cli")
        state = task_continuation.status()

        self.assertNotIn(LEGACY_EXHAUSTION_TEXT, result)
        self.assertIn("已保存可恢复检查点", result)
        self.assertTrue(state["exists"])
        self.assertEqual(state["status"], "pending")
        self.assertEqual(state["reason"], "automatic_tool_budget_exhaustion")
        self.assertIn(str(state["id"]), result)
        self.assertEqual(state["task_summary"], "继续修复自主进化模块")

    def test_runtime_binding_is_idempotent(self):
        agent = self._agent(rounds=2, segments=2)
        first = agent.chat
        tool_budget_continuation.configure_runtime(agent=agent, config=agent.config)
        second = agent.chat

        self.assertIs(first.__func__, second.__func__)
        self.assertEqual(agent.max_tool_segments, 2)
        self.assertEqual(agent.max_total_tool_rounds, 4)

    def test_segment_configuration_is_bounded_and_environment_can_override(self):
        self.assertEqual(configured_segments({"agent": {"max_tool_segments": 0}}), 1)
        self.assertEqual(configured_segments({"agent": {"max_tool_segments": 99}}), 16)
        os.environ["AGENELF_MAX_TOOL_SEGMENTS"] = "6"
        self.assertEqual(configured_segments({"agent": {"max_tool_segments": 2}}), 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
