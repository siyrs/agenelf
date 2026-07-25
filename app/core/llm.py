"""LLM 客户端模块。

提供基于 OpenAI 兼容接口的 LLMClient，以及无 API Key 时
用于本地开发与测试的脚本化 MockLLM。
"""

from __future__ import annotations

import json
import os

# 触发 MockLLM 生成工具调用的中文关键词
_TRIGGER_KEYWORDS = ("写", "代码", "运行", "执行", "文件")

# 自主循环补丁请求的特征文本（对应 core/autonomy.py 的补丁提示词；
# 这里硬编码匹配，避免 import autonomy 造成循环依赖）
_AUTONOMY_PROMPT_MARKERS = ("受控自主改进执行器", "【硬性输出契约】")

# MockLLM 离线自主补丁演示：growth_pulse 技能完整源码（纯标准库）
_MOCK_GROWTH_PULSE_SKILL = '''"""成长脉动技能：离线自主迭代演示产物。

返回一句带 UTC 时间戳的中文"成长脉动"文本；
当前技能数等运行事实可通过参数传入。纯标准库实现，不依赖其他技能。
"""

from __future__ import annotations

from datetime import datetime, timezone

SKILL_META = {
    "name": "growth_pulse",
    "description": "生成一句带时间戳的中文成长脉动文本，用于标记一次可验证的自我迭代。",
    "version": "0.1.0",
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "growth_pulse",
            "description": "返回一句带 UTC 时间戳的成长脉动文本，可附带主题与当前技能数。",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "可选主题词，默认空字符串。",
                    },
                    "skill_count": {
                        "type": "integer",
                        "description": "可选当前已加载技能数，不大于 0 时省略。",
                    },
                },
                "required": [],
            },
        },
    }
]


def _growth_pulse(args: dict) -> str:
    args = args if isinstance(args, dict) else {}
    topic = str(args.get("topic", "") or "").strip()
    try:
        skill_count = int(args.get("skill_count", 0) or 0)
    except (TypeError, ValueError):
        skill_count = 0
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    text = f"[{stamp}] 成长脉动"
    if topic:
        text += f"·{topic}"
    text += "：Agenelf 又完成一次小而可验证的前进"
    if skill_count > 0:
        text += f"，当前已加载 {skill_count} 个技能"
    return text + "。"


def execute(tool_name: str, args: dict) -> str:
    if tool_name == "growth_pulse":
        return _growth_pulse(args)
    known = ", ".join(sorted(t["function"]["name"] for t in TOOLS))
    return f"未知工具：{tool_name}，可用工具：{known}"
'''

# MockLLM 离线自主补丁演示：growth_pulse 对应测试完整源码（独立可过）
_MOCK_GROWTH_PULSE_TEST = '''"""growth_pulse 技能的协议与行为测试（离线自主补丁演示产物）。"""

from __future__ import annotations

import unittest

from skills import growth_pulse


class GrowthPulseSkillTest(unittest.TestCase):
    """校验技能协议三件套与 execute 行为，独立可过、不依赖其他技能。"""

    def test_skill_meta(self):
        self.assertEqual(growth_pulse.SKILL_META["name"], "growth_pulse")
        self.assertTrue(growth_pulse.SKILL_META["description"])
        self.assertTrue(growth_pulse.SKILL_META["version"])

    def test_tools_schema(self):
        self.assertIsInstance(growth_pulse.TOOLS, list)
        self.assertEqual(len(growth_pulse.TOOLS), 1)
        function = growth_pulse.TOOLS[0]["function"]
        self.assertEqual(function["name"], "growth_pulse")
        self.assertEqual(function["parameters"]["type"], "object")
        self.assertIn("topic", function["parameters"]["properties"])

    def test_execute_returns_pulse_text(self):
        result = growth_pulse.execute(
            "growth_pulse", {"topic": "离线演示", "skill_count": 3}
        )
        self.assertIsInstance(result, str)
        self.assertTrue(result.strip())
        self.assertIn("成长脉动", result)
        self.assertIn("离线演示", result)
        self.assertIn("3 个技能", result)

    def test_execute_defaults_and_unknown_tool(self):
        self.assertTrue(growth_pulse.execute("growth_pulse", {}).strip())
        self.assertIn("未知工具", growth_pulse.execute("missing", {}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
'''

# 脚本化自主补丁响应：仅含两个合规代码块，无多余解释
_AUTONOMY_PATCH_RESPONSE = (
    "```python\n# FILE: skills/growth_pulse.py\n" + _MOCK_GROWTH_PULSE_SKILL + "```\n"
    "\n"
    "```python\n# FILE: tests/test_growth_pulse.py\n" + _MOCK_GROWTH_PULSE_TEST + "```\n"
)


class LLMClient:
    """OpenAI 兼容聊天接口客户端。

    config 期望包含 llm 相关配置，支持两种形态：
    - {"llm": {"base_url": ..., "api_key": ..., "model": ..., "temperature": ...}}
    - 直接传入 llm 子字典本身
    api_key 为空时回退读取环境变量 OPENAI_API_KEY。
    """

    def __init__(self, config: dict):
        # 兼容完整配置或仅 llm 段配置两种传入方式
        llm_cfg = config.get("llm", config)
        self.base_url = llm_cfg.get("base_url", "https://api.moonshot.cn/v1")
        self.api_key = llm_cfg.get("api_key") or os.environ.get("OPENAI_API_KEY", "")
        self.model = llm_cfg.get("model", "kimi-k2-0905-preview")
        self.temperature = llm_cfg.get("temperature", 0.6)

        # 延迟创建 OpenAI 客户端，避免无 Key 场景下 import/初始化失败
        self._client = None
        if self.api_key:
            from openai import OpenAI

            self._client = OpenAI(base_url=self.base_url, api_key=self.api_key)

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """发起一轮聊天补全。

        返回统一格式：
        {"content": str | None,
         "tool_calls": [{"id": str, "name": str, "arguments": dict}]}
        """
        if self._client is None:
            raise RuntimeError("LLMClient 未配置 api_key，请使用 MockLLM")

        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        resp = self._client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message

        tool_calls: list[dict] = []
        for tc in msg.tool_calls or []:
            # arguments 是 JSON 字符串，解析失败时回退为空 dict
            try:
                arguments = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}
            tool_calls.append(
                {"id": tc.id, "name": tc.function.name, "arguments": arguments}
            )
        return {"content": msg.content, "tool_calls": tool_calls}


class MockLLM(LLMClient):
    """脚本化的假 LLM，用于无 API Key 的本地开发与测试。

    行为脚本：
    0. 任一消息内容包含自主循环特征文本（"受控自主改进执行器" 或
       "【硬性输出契约】"）时，返回一个脚本化但完全合规的自主补丁：
       content 内含 skills/growth_pulse.py 与 tests/test_growth_pulse.py
       两个 ```python 代码块（首行 # FILE: 标记），tool_calls 为空，
       使离线环境也能端到端演示受控自主迭代；
    1. 首轮用户输入中若含 "写"/"代码"/"运行" 等关键词，
       返回一个 write_code_file 或 run_python 的 tool_call；
    2. 若对话中已包含工具结果（role == "tool"），返回最终文本回复；
    3. 其他情况返回普通的提示文本。
    """

    def __init__(self, config: dict | None = None):
        # 不调用父类 __init__，避免任何网络客户端初始化
        self.config = config or {}
        self.model = "mock-llm"
        # 记录 chat 调用次数，便于调试与测试
        self.call_count = 0

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        self.call_count += 1

        # 自主循环补丁请求：命中特征文本时返回脚本化的合规补丁，
        # 保证无 API Key 的离线环境也能演示完整的自主迭代流程
        if any(
            marker in str(m.get("content", ""))
            for m in messages
            for marker in _AUTONOMY_PROMPT_MARKERS
        ):
            return {"content": _AUTONOMY_PATCH_RESPONSE, "tool_calls": []}

        # 对话中已存在工具结果 → 生成最终总结回复；
        # 若刚写完示例文件且 run_python 可用，则再补一步"运行它"，演示多轮工具调用
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        if tool_msgs:
            last_result = str(tool_msgs[-1].get("content", ""))
            available = set()
            for t in tools or []:
                fn = t.get("function", {}) if isinstance(t, dict) else {}
                if fn.get("name"):
                    available.add(fn["name"])
            already_ran = any("退出码" in str(m.get("content", "")) for m in tool_msgs)
            if (
                not already_ran
                and "run_python" in available
                and "hello.py" in last_result
                and "写入失败" not in last_result
            ):
                return {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "mock_call_run",
                            "name": "run_python",
                            "arguments": {"code": "exec(open('hello.py', encoding='utf-8').read())"},
                        }
                    ],
                }
            return {
                "content": f"任务已完成，工具执行结果如下：\n{last_result}",
                "tool_calls": [],
            }

        # 取最后一条用户输入判断是否要触发工具调用
        user_text = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_text = str(m.get("content", ""))
                break

        available = set()
        for t in tools or []:
            # OpenAI schema: {"type": "function", "function": {"name": ...}}
            fn = t.get("function", {}) if isinstance(t, dict) else {}
            if fn.get("name"):
                available.add(fn["name"])

        if any(kw in user_text for kw in _TRIGGER_KEYWORDS):
            # 优先调用 write_code_file，其次 run_python；均不可用时伪造一个，
            # 由 registry.dispatch 报未知工具，也能走完一轮完整 tool-call 回路
            if "run_python" in available and "write_code_file" not in available:
                return {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "mock_call_1",
                            "name": "run_python",
                            "arguments": {"code": "print('hello world')"},
                        }
                    ],
                }
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": "mock_call_1",
                        "name": "write_code_file",
                        "arguments": {
                            "path": "hello.py",
                            "content": "print('hello world')\n",
                        },
                    }
                ],
            }

        return {
            "content": "（MockLLM）未识别到可触发工具的关键词，请描述需要写代码或运行的任务。",
            "tool_calls": [],
        }
