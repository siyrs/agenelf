from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.agent import Agent
from skills import (
    evolution_scope_guard,
    task_continuation,
    tool_budget_continuation,
    zz_transport_resilience,
)


class RemoteProtocolError(Exception):
    pass


class FlakyLLM:
    def __init__(self):
        self._agenelf_stream_reasoning = True
        self.flags: list[bool] = []
        self.calls = 0

    def chat(self, messages, tools=None):
        del messages, tools
        self.calls += 1
        self.flags.append(bool(self._agenelf_stream_reasoning))
        if self.calls == 1:
            raise RemoteProtocolError(
                "peer closed connection without sending complete message body "
                "(incomplete chunked read)"
            )
        return {"content": "已从非流式响应恢复", "tool_calls": []}


class BrokenLLM:
    model = "broken"
    temperature = 0.0

    def chat(self, messages, tools=None):
        del messages, tools
        raise RuntimeError("provider unavailable")


class RepeatingLLM:
    model = "repeat"
    temperature = 0.0

    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools=None):
        del messages, tools
        self.calls += 1
        return {
            "content": None,
            "tool_calls": [
                {
                    "id": f"call-{self.calls}",
                    "name": "evolution_begin",
                    "arguments": {"goal": "添加 docker down"},
                }
            ],
        }


class RuntimeFailureRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.skills = self.root / "skills"
        self.skills.mkdir()
        self.old_root = os.environ.get("AGENELF_ROOT")
        os.environ["AGENELF_ROOT"] = str(self.root)

    def tearDown(self):
        if self.old_root is None:
            os.environ.pop("AGENELF_ROOT", None)
        else:
            os.environ["AGENELF_ROOT"] = self.old_root
        self.tmp.cleanup()

    def _agent(self, rounds=8, segments=1) -> Agent:
        agent = Agent(
            {
                "mock": True,
                "runtime_root": str(self.root),
                "skills_dir": str(self.skills),
                "memory_path": str(self.root / "memory.json"),
                "persona_path": str(self.root / "persona.yaml"),
                "agent": {
                    "max_tool_rounds": rounds,
                    "max_tool_segments": segments,
                    "no_progress_repeat_limit": 3,
                    "history_max_messages": 4,
                },
            }
        )
        agent._maybe_auto_reflect = lambda: None
        tool_budget_continuation.configure_runtime(agent=agent, config=agent.config)
        return agent

    def test_interrupted_stream_retries_once_without_streaming(self):
        llm = FlakyLLM()
        agent = SimpleNamespace(
            llm=llm,
            config={
                "llm": {
                    "transport_retry_attempts": 2,
                    "transport_retry_backoff_seconds": 0,
                    "stream_fallback_non_stream": True,
                }
            },
        )
        zz_transport_resilience.configure_runtime(agent=agent, config=agent.config)
        result = llm.chat([{"role": "user", "content": "hello"}])
        self.assertEqual(result["content"], "已从非流式响应恢复")
        self.assertEqual(llm.flags, [True, False])
        self.assertTrue(llm._agenelf_stream_reasoning)

    def test_non_transient_error_is_not_retried(self):
        class InvalidLLM:
            def __init__(self):
                self.calls = 0

            def chat(self, messages, tools=None):
                del messages, tools
                self.calls += 1
                raise ValueError("invalid request")

        llm = InvalidLLM()
        agent = SimpleNamespace(
            llm=llm,
            config={"llm": {"transport_retry_attempts": 4}},
        )
        zz_transport_resilience.configure_runtime(agent=agent, config=agent.config)
        with self.assertRaises(ValueError):
            llm.chat([])
        self.assertEqual(llm.calls, 1)

    def test_unrecovered_model_failure_keeps_cli_task_recoverable(self):
        agent = self._agent(rounds=2, segments=2)
        agent.llm = BrokenLLM()
        result = agent.chat("继续修复 VPN", subject="cli")
        state = task_continuation.status()
        self.assertIn("CLI 没有退出", result)
        self.assertIn("已保存可恢复检查点", result)
        self.assertTrue(state["exists"])
        self.assertEqual(state["reason"], "llm_request_failure")

    def test_repeated_identical_tool_failure_stops_before_round_budget(self):
        agent = self._agent(rounds=10, segments=1)
        agent.llm = RepeatingLLM()
        agent.registry.dispatch = (
            lambda name, args, subject="agent":
            "baseline_failed: existing tests and CI fixtures are unavailable"
        )
        result = agent.chat("自我迭代 docker down", subject="cli")
        state = task_continuation.status()
        self.assertIn("无进展循环", result)
        self.assertEqual(agent.llm.calls, 3)
        self.assertEqual(state["reason"], "automatic_no_progress_loop")

    def test_protected_goal_is_classified_into_owner_authorized_upgrade(self):
        calls: list[tuple[str, bool]] = []

        class FakeAgent:
            config = {"runtime_root": str(self.root)}

            def run_autonomy_cycle(self, goal="", apply_changes=False):
                calls.append((goal, apply_changes))
                return {"status": "promotion_requested"}

        fake = FakeAgent()
        evolution_scope_guard.configure_runtime(agent=fake)
        routed = {
            "id": "upgrade-20260726-120000-12345678",
            "status": "awaiting_intent_approval",
            "next_action": "/approve auth-123456789abc",
        }
        with patch(
            "skills.authorized_self_upgrade.route_goal",
            return_value=routed,
        ) as route:
            result = fake.run_autonomy_cycle(
                "为 docker compose down 修改 ops runner 和审批策略",
                apply_changes=True,
            )

        self.assertEqual(result["status"], "awaiting_intent_approval")
        self.assertNotEqual(result["status"], "host_review_required")
        self.assertIn("runners", result["matched_protected_scopes"])
        self.assertIn("compose", result["matched_protected_scopes"])
        self.assertIn("authorization_control", result["matched_protected_scopes"])
        self.assertFalse(calls)
        route.assert_called_once()

        normal = fake.run_autonomy_cycle(
            "改进一个非保护的文本格式化技能",
            apply_changes=True,
        )
        self.assertEqual(normal["status"], "promotion_requested")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
