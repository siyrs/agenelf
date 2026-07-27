from __future__ import annotations

import io
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from rich.console import Console

from core.reasoning_trace import (
    ReasoningPanelRenderer,
    install_reasoning_trace,
)
from skills import reasoning_trace as reasoning_skill


def ns(**kwargs):
    return SimpleNamespace(**kwargs)


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FakeLLM:
    model = "deepseek-v4-pro"
    temperature = 0.6

    def __init__(self, responses=None):
        self._client = None
        if responses is not None:
            self._client = ns(
                chat=ns(
                    completions=FakeCompletions(responses)
                )
            )

    def chat(self, messages, tools=None):
        del messages, tools
        return {"content": "fallback", "tool_calls": []}


class ReasoningTraceTest(unittest.TestCase):
    def test_streams_reasoning_and_round_trips_it_for_tool_calls(self):
        first_stream = iter(
            [
                ns(
                    choices=[
                        ns(
                            delta=ns(
                                reasoning_content="先检查",
                                content=None,
                                tool_calls=[],
                            )
                        )
                    ]
                ),
                ns(
                    choices=[
                        ns(
                            delta=ns(
                                reasoning_content="配置。",
                                content=None,
                                tool_calls=[],
                            )
                        )
                    ]
                ),
                ns(
                    choices=[
                        ns(
                            delta=ns(
                                reasoning_content=None,
                                content=None,
                                tool_calls=[
                                    ns(
                                        index=0,
                                        id="call-1",
                                        function=ns(
                                            name=(
                                                "inspect_docker_"
                                                "container"
                                            ),
                                            arguments=(
                                                '{"target":'
                                                '"pve-ubuntu",'
                                            ),
                                        ),
                                    )
                                ],
                            )
                        )
                    ]
                ),
                ns(
                    choices=[
                        ns(
                            delta=ns(
                                reasoning_content=None,
                                content=None,
                                tool_calls=[
                                    ns(
                                        index=0,
                                        id=None,
                                        function=ns(
                                            name=None,
                                            arguments=(
                                                '"container":'
                                                '"sing-box"}'
                                            ),
                                        ),
                                    )
                                ],
                            )
                        )
                    ]
                ),
            ]
        )
        second_stream = iter(
            [
                ns(
                    choices=[
                        ns(
                            delta=ns(
                                reasoning_content=(
                                    "工具结果正常。"
                                ),
                                content=None,
                                tool_calls=[],
                            )
                        )
                    ]
                ),
                ns(
                    choices=[
                        ns(
                            delta=ns(
                                reasoning_content=None,
                                content="VPN 已恢复",
                                tool_calls=[],
                            )
                        )
                    ]
                ),
            ]
        )
        llm = FakeLLM([first_stream, second_stream])
        events = []
        install_reasoning_trace(
            llm,
            {"llm": {"stream_reasoning": True}},
            listener=events.append,
        )
        messages = [
            {"role": "user", "content": "检查 VPN"}
        ]
        first = llm.chat(
            messages,
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "inspect_docker_container"
                    },
                }
            ],
        )
        self.assertEqual(
            first["reasoning_content"], "先检查配置。"
        )
        self.assertEqual(first["tool_calls"][0]["id"], "call-1")
        self.assertEqual(
            first["tool_calls"][0]["arguments"]["container"],
            "sing-box",
        )

        messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": (
                                "inspect_docker_container"
                            ),
                            "arguments": (
                                '{"target":"pve-ubuntu",'
                                '"container":"sing-box"}'
                            ),
                        },
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": "ok",
            }
        )
        second = llm.chat(messages, tools=[])
        self.assertEqual(second["content"], "VPN 已恢复")
        sent_messages = (
            llm._client.chat.completions.calls[1]["messages"]
        )
        self.assertEqual(
            sent_messages[1]["reasoning_content"],
            "先检查配置。",
        )
        self.assertTrue(
            any(
                item["type"] == "reasoning_delta"
                for item in events
            )
        )
        self.assertEqual(
            [
                item["round"]
                for item in events
                if item["type"] == "reasoning_started"
            ],
            [1, 2],
        )

    def test_non_stream_reads_reasoning_from_model_extra(self):
        message = ns(
            content="最终答案",
            tool_calls=[],
            model_extra={
                "reasoning_content": "供应商推理"
            },
        )
        response = ns(choices=[ns(message=message)])
        llm = FakeLLM([response])
        install_reasoning_trace(
            llm,
            {"llm": {"stream_reasoning": False}},
        )
        result = llm.chat(
            [{"role": "user", "content": "问题"}]
        )
        self.assertEqual(
            result["reasoning_content"], "供应商推理"
        )
        self.assertEqual(result["content"], "最终答案")

    def test_display_events_are_redacted(self):
        response = ns(
            choices=[
                ns(
                    message=ns(
                        content="ok",
                        tool_calls=[],
                        reasoning_content=(
                            "检查 token=supersecret 和 "
                            "vless://uuid@example.com:443"
                            "?password=abc"
                        ),
                    )
                )
            ]
        )
        llm = FakeLLM([response])
        events = []
        install_reasoning_trace(
            llm,
            {"llm": {"stream_reasoning": False}},
            listener=events.append,
        )
        llm.chat(
            [{"role": "user", "content": "问题"}]
        )
        text = next(
            item["text"]
            for item in events
            if item["type"] == "reasoning_completed"
        )
        self.assertNotIn("supersecret", text)
        self.assertNotIn("uuid@example.com", text)
        self.assertIn("[REDACTED]", text)

    def test_renderer_uses_distinct_terminal_style(self):
        output = io.StringIO()
        console = Console(
            file=output,
            force_terminal=True,
            width=100,
        )
        renderer = ReasoningPanelRenderer(
            console=console,
            max_chars=10_000,
        )
        renderer.handle(
            {"type": "reasoning_started", "round": 2}
        )
        renderer.handle(
            {
                "type": "reasoning_delta",
                "round": 2,
                "text": "先读取日志，再检查配置。",
            }
        )
        panel = renderer.panel()
        self.assertEqual(str(panel.border_style), "cyan")
        self.assertEqual(
            str(panel.renderable.style),
            "italic dim bright_cyan",
        )
        renderer.handle(
            {
                "type": "reasoning_completed",
                "round": 2,
                "text": "先读取日志，再检查配置。",
            }
        )
        rendered = output.getvalue()
        self.assertIn("思考过程", rendered)
        self.assertIn("先读取日志", rendered)

    def test_skill_installation_is_idempotent(self):
        llm = FakeLLM()
        agent = ns(
            llm=llm,
            config={"cli": {"show_reasoning": False}},
        )
        reasoning_skill.configure_runtime(
            agent=agent,
            config=agent.config,
        )
        first = llm.chat
        reasoning_skill.configure_runtime(
            agent=agent,
            config=agent.config,
        )
        second = llm.chat
        self.assertIs(first.__func__, second.__func__)
        self.assertTrue(
            llm._agenelf_reasoning_trace_installed
        )

    def test_auto_display_can_be_disabled(self):
        llm = FakeLLM()
        with patch.dict(
            os.environ,
            {"AGENELF_SHOW_REASONING": "0"},
            clear=False,
        ):
            install_reasoning_trace(
                llm,
                {"cli": {"show_reasoning": True}},
            )
        self.assertIsNone(
            llm._agenelf_reasoning_listener
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
