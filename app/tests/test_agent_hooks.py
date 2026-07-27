"""Agent 显式有序钩子管线与合并后单一主回路的行为测试。

覆盖：
- add_llm_wrapper 的 priority 排序（数值越大越外层）与同名覆盖幂等；
- add_cycle_guard 的 priority 排序与幂等；
- list_hooks 诊断输出（最外层在前）；
- 合并后的 Agent.chat 本体即具备分段预算/续跑能力（无需任何安装步骤）。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from core.agent import Agent


class ScriptedLLM:
    model = "scripted"
    temperature = 0.0

    def __init__(self, responses: list[dict]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def chat(self, messages, tools=None):
        self.calls.append(
            json.loads(json.dumps(messages, ensure_ascii=False))
        )
        if not self.responses:
            raise AssertionError("ScriptedLLM response budget exhausted")
        return self.responses.pop(0)


def _tool_response(index: int) -> dict:
    return {
        "content": None,
        "tool_calls": [
            {"id": f"call-{index}", "name": "step", "arguments": {"index": index}}
        ],
    }


class AgentHooksTest(unittest.TestCase):
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

    def _agent(self, **agent_cfg) -> Agent:
        config = {
            "mock": True,
            "runtime_root": str(self.root),
            "skills_dir": str(self.skills_dir),
            "memory_path": str(self.root / "memory.json"),
            "persona_path": str(self.root / "persona.yaml"),
            "agent": {"history_max_messages": 8, **agent_cfg},
        }
        agent = Agent(config)
        agent._maybe_auto_reflect = lambda: None
        return agent

    # ------------------------------------------------------------------
    # LLM wrapper 管线
    # ------------------------------------------------------------------
    def test_llm_wrappers_apply_in_priority_order_larger_is_outer(self):
        agent = self._agent()
        events: list[str] = []

        def make_wrapper(tag):
            def wrapper(call_next, messages, tools=None):
                events.append(f"{tag}-before")
                try:
                    return call_next(messages, tools=tools)
                finally:
                    events.append(f"{tag}-after")

            return wrapper

        agent.add_llm_wrapper(make_wrapper("p1000"), priority=1000, name="w-outer")
        agent.add_llm_wrapper(make_wrapper("p10"), priority=10, name="w-inner")
        agent.add_llm_wrapper(make_wrapper("p100"), priority=100, name="w-middle")

        llm = ScriptedLLM([{"content": "ok", "tool_calls": []}])
        agent.llm = llm
        result = agent._call_llm([{"role": "user", "content": "hi"}])

        self.assertEqual(result["content"], "ok")
        self.assertEqual(
            events,
            [
                "p1000-before",
                "p100-before",
                "p10-before",
                "p10-after",
                "p100-after",
                "p1000-after",
            ],
        )

    def test_llm_wrapper_same_name_replaces_instead_of_stacking(self):
        agent = self._agent()
        calls: list[str] = []

        def first(call_next, messages, tools=None):
            calls.append("first")
            return call_next(messages, tools=tools)

        def second(call_next, messages, tools=None):
            calls.append("second")
            return call_next(messages, tools=tools)

        agent.add_llm_wrapper(first, priority=100, name="dup")
        agent.add_llm_wrapper(second, priority=100, name="dup")

        llm = ScriptedLLM([{"content": "ok", "tool_calls": []}])
        agent.llm = llm
        agent._call_llm([{"role": "user", "content": "hi"}])

        self.assertEqual(calls, ["second"])
        self.assertEqual(len(llm.calls), 1)
        wrappers = agent.list_hooks()["llm_wrappers"]
        self.assertEqual([row["name"] for row in wrappers], ["dup"])

    def test_list_hooks_reports_outermost_first(self):
        agent = self._agent()
        agent.add_llm_wrapper(lambda nxt, m, tools=None: nxt(m, tools=tools),
                              priority=10, name="low")
        agent.add_llm_wrapper(lambda nxt, m, tools=None: nxt(m, tools=tools),
                              priority=900, name="high")
        agent.add_cycle_guard(
            lambda nxt, goal="", apply_changes=False: nxt(
                goal=goal, apply_changes=apply_changes
            ),
            priority=100,
            name="guard",
        )
        hooks = agent.list_hooks()
        self.assertEqual(
            hooks["llm_wrappers"],
            [
                {"name": "high", "priority": 900},
                {"name": "low", "priority": 10},
            ],
        )
        self.assertEqual(
            hooks["cycle_guards"], [{"name": "guard", "priority": 100}]
        )

    def test_wrapper_composition_survives_llm_replacement(self):
        """包装器延迟绑定 self.llm：运行期替换 LLM 后钩子依然生效。"""

        agent = self._agent()
        seen: list[str] = []

        def wrapper(call_next, messages, tools=None):
            seen.append("wrapped")
            return call_next(messages, tools=tools)

        agent.add_llm_wrapper(wrapper, priority=100, name="trace")
        agent.llm = ScriptedLLM([{"content": "new-llm", "tool_calls": []}])
        result = agent._call_llm([{"role": "user", "content": "hi"}])
        self.assertEqual(result["content"], "new-llm")
        self.assertEqual(seen, ["wrapped"])

    # ------------------------------------------------------------------
    # cycle guard 管线
    # ------------------------------------------------------------------
    def test_cycle_guards_apply_in_priority_order_and_are_idempotent(self):
        agent = self._agent()
        events: list[str] = []

        def make_guard(tag):
            def guard(call_next, goal="", apply_changes=False):
                events.append(tag)
                return call_next(goal=goal, apply_changes=apply_changes)

            return guard

        agent.add_cycle_guard(make_guard("outer"), priority=200, name="g-outer")
        agent.add_cycle_guard(make_guard("inner"), priority=50, name="g-inner")
        agent.add_cycle_guard(make_guard("inner-v2"), priority=50, name="g-inner")

        # 避免真正执行自治循环：最内层之后直接由替身守卫短路
        def short_circuit(call_next, goal="", apply_changes=False):
            del call_next
            return {"status": "short-circuited", "goal": goal}

        agent.add_cycle_guard(short_circuit, priority=10, name="g-core")

        result = agent.run_autonomy_cycle("普通目标", apply_changes=False)
        self.assertEqual(result["status"], "short-circuited")
        self.assertEqual(events, ["outer", "inner-v2"])

    # ------------------------------------------------------------------
    # 合并后的单一主回路
    # ------------------------------------------------------------------
    def test_chat_is_single_builtin_loop_without_any_installation(self):
        """不调用任何 configure/install，Agent.chat 本体即支持分段续跑。"""

        agent = self._agent(max_tool_rounds=2, max_tool_segments=2)
        self.assertIs(agent.chat.__func__, Agent.chat)

        agent.llm = ScriptedLLM(
            [
                _tool_response(1),
                _tool_response(2),
                {"content": "跨段完成", "tool_calls": []},
            ]
        )
        agent.registry.dispatch = (
            lambda name, args, subject="agent": f"ok-{args['index']}"
        )

        result = agent.chat("长任务", subject="cli")

        self.assertEqual(result, "跨段完成")
        self.assertEqual(agent.max_total_tool_rounds, 4)
        third_messages = agent.llm.calls[2]
        self.assertIn("运行时自动续跑", third_messages[0]["content"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
