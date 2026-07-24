"""LLM 客户端模块。

提供基于 OpenAI 兼容接口的 LLMClient，以及无 API Key 时
用于本地开发与测试的脚本化 MockLLM。
"""

from __future__ import annotations

import json
import os

# 触发 MockLLM 生成工具调用的中文关键词
_TRIGGER_KEYWORDS = ("写", "代码", "运行", "执行", "文件")


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
