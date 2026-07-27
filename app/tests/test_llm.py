"""core/llm.py 与 core/mock_llm.py 的主路径测试。

覆盖：
- LLMClient 初始化（完整配置 / 仅 llm 段两种形态）、无 API Key 回退行为；
- MockLLM 关键词触发工具调用、工具结果后的总结回复、普通消息回显；
- 自主循环特征文本命中时返回脚本化合规补丁；
- 补丁内容通过运行时动态 import skills.growth_pulse 生成，
  与仓库内真实技能源码保持一致。
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import unittest
from unittest import mock

from core.llm import LLMClient, _sanitize_surrogates
from core.mock_llm import MockLLM, _build_autonomy_patch_response

_WRITE_CODE_TOOL = {
    "type": "function",
    "function": {"name": "write_code_file", "parameters": {"type": "object"}},
}
_RUN_PYTHON_TOOL = {
    "type": "function",
    "function": {"name": "run_python", "parameters": {"type": "object"}},
}


def _patch_request_messages() -> list[dict]:
    return [
        {"role": "system", "content": "你是严谨的 Python 工程师。"},
        {
            "role": "user",
            "content": (
                "你是 Agenelf 的受控自主改进执行器。\n"
                "【硬性输出契约】\n仅输出 ```python 代码块。"
            ),
        },
    ]


class LLMClientInitTest(unittest.TestCase):
    """LLMClient 初始化与无 Key 回退路径。"""

    def test_full_config_form(self):
        client = LLMClient(
            {
                "llm": {
                    "base_url": "https://example.com/v1",
                    "model": "demo-model",
                    "temperature": 0.1,
                }
            }
        )
        self.assertEqual(client.base_url, "https://example.com/v1")
        self.assertEqual(client.model, "demo-model")
        self.assertEqual(client.temperature, 0.1)

    def test_bare_llm_section_form_and_defaults(self):
        client = LLMClient({"model": "m"})
        self.assertEqual(client.model, "m")
        self.assertEqual(client.base_url, "https://api.moonshot.cn/v1")
        self.assertEqual(client.temperature, 0.6)

    def test_no_api_key_falls_back_to_no_client(self):
        # 配置与环境变量均无 Key：不创建 OpenAI 客户端，chat 明确报错
        with mock.patch.dict(os.environ, {}, clear=True):
            client = LLMClient({"llm": {}})
        self.assertEqual(client.api_key, "")
        self.assertIsNone(client._client)
        with self.assertRaises(RuntimeError):
            client.chat([{"role": "user", "content": "hi"}])

    def test_api_key_from_environment(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "env-key"}):
            if importlib.util.find_spec("openai") is None:
                # 环境未安装 openai：有 Key 时延迟 import 才失败，证明确实走了 Key 分支
                with self.assertRaises(ModuleNotFoundError):
                    LLMClient({"llm": {}})
            else:
                client = LLMClient({"llm": {}})
                self.assertEqual(client.api_key, "env-key")
                self.assertIsNotNone(client._client)

    def test_sanitize_surrogates_recursive(self):
        dirty = {
            "s": "a\ud800b",
            "list": ["\udfff", 1],
            "nested": {"x": "\ud83d"},
            "num": 3,
        }
        clean = _sanitize_surrogates(dirty)
        self.assertEqual(clean["s"], "a�b")
        self.assertEqual(clean["list"], ["�", 1])
        self.assertEqual(clean["nested"]["x"], "�")
        self.assertEqual(clean["num"], 3)


class MockLLMChatTest(unittest.TestCase):
    """MockLLM 常规对话行为。"""

    def test_init_defaults_and_call_count(self):
        llm = MockLLM()
        self.assertEqual(llm.model, "mock-llm")
        self.assertEqual(llm.config, {})
        self.assertEqual(llm.call_count, 0)
        llm.chat([{"role": "user", "content": "你好"}])
        llm.chat([{"role": "user", "content": "你好"}])
        self.assertEqual(llm.call_count, 2)

    def test_keyword_triggers_write_code_file(self):
        response = MockLLM().chat(
            [{"role": "user", "content": "帮我写一个脚本"}],
            tools=[_WRITE_CODE_TOOL, _RUN_PYTHON_TOOL],
        )
        self.assertIsNone(response["content"])
        call = response["tool_calls"][0]
        self.assertEqual(call["name"], "write_code_file")
        self.assertEqual(call["arguments"]["path"], "hello.py")

    def test_keyword_prefers_run_python_when_write_unavailable(self):
        response = MockLLM().chat(
            [{"role": "user", "content": "运行一下代码"}],
            tools=[_RUN_PYTHON_TOOL],
        )
        call = response["tool_calls"][0]
        self.assertEqual(call["name"], "run_python")
        self.assertIn("print", call["arguments"]["code"])

    def test_tool_result_yields_final_summary(self):
        messages = [
            {"role": "user", "content": "写个文件"},
            {"role": "tool", "content": "已写入 hello.py（写入失败：无）"},
        ]
        # 结果含 "写入失败" → 不再追加 run_python，直接总结
        response = MockLLM().chat(messages, tools=[_RUN_PYTHON_TOOL])
        self.assertEqual(response["tool_calls"], [])
        self.assertIn("任务已完成", response["content"])

    def test_tool_result_then_run_python_demo_step(self):
        messages = [
            {"role": "user", "content": "写个文件"},
            {"role": "tool", "content": "已写入 hello.py"},
        ]
        response = MockLLM().chat(messages, tools=[_RUN_PYTHON_TOOL])
        call = response["tool_calls"][0]
        self.assertEqual(call["name"], "run_python")
        self.assertIn("hello.py", call["arguments"]["code"])

    def test_plain_message_echoes_hint(self):
        response = MockLLM().chat(
            [{"role": "user", "content": "今天天气如何？"}], tools=None
        )
        self.assertEqual(response["tool_calls"], [])
        self.assertIn("MockLLM", response["content"])
        self.assertNotIn("# FILE:", response["content"])


class MockLLMAutonomyPatchTest(unittest.TestCase):
    """自主提示词匹配与动态 import growth_pulse 生成补丁。"""

    def test_autonomy_markers_return_demo_patch(self):
        response = MockLLM().chat(_patch_request_messages(), tools=None)
        self.assertEqual(response["tool_calls"], [])
        content = response["content"]
        self.assertIn("# FILE: skills/growth_pulse.py", content)
        self.assertIn("# FILE: tests/test_growth_pulse.py", content)

    def test_single_marker_also_matches(self):
        response = MockLLM().chat(
            [{"role": "user", "content": "【硬性输出契约】只输出代码块"}]
        )
        self.assertIn("# FILE:", response["content"])

    def test_patch_uses_live_growth_pulse_source(self):
        # 补丁中的技能源码必须与仓库内真实模块源码逐字一致（动态 import）
        from skills import growth_pulse

        content = _build_autonomy_patch_response()
        self.assertIn(inspect.getsource(growth_pulse), content)
        self.assertEqual(growth_pulse.SKILL_META["name"], "growth_pulse")
        self.assertTrue(callable(growth_pulse.execute))

    def test_dynamic_import_of_growth_pulse_succeeds(self):
        from skills import growth_pulse

        result = growth_pulse.execute(
            "growth_pulse", {"topic": "测试", "skill_count": 2}
        )
        self.assertIn("成长脉动", result)
        self.assertIn("测试", result)
        self.assertIn("2 个技能", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
