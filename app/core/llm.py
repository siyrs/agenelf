"""LLM 客户端模块。

提供基于 OpenAI 兼容接口的真实 LLMClient。无 API Key 时用于本地
开发与测试的脚本化 MockLLM 见 core/mock_llm.py。
"""

from __future__ import annotations

import json
import os
import re

# 清洗字符串中的孤立 surrogate 字符（\ud800-\udfff），避免 openai 库 json 序列化崩溃
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def _sanitize_surrogates(obj: object) -> object:
    """递归清洗对象中所有字符串的 surrogate 字符。"""
    if isinstance(obj, str):
        return _SURROGATE_RE.sub("�", obj)
    if isinstance(obj, dict):
        return {k: _sanitize_surrogates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_surrogates(v) for v in obj]
    return obj


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
        # 防御性清洗：递归去除所有字符串中的 surrogate 字符，防止 openai JSON 编码崩溃
        kwargs = _sanitize_surrogates(kwargs)
        resp = self._client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message

        # 清洗 LLM 响应中的 surrogate 字符（DeepSeek 等模型可能返回含 \ud800-\udfff 的文本）
        content = _SURROGATE_RE.sub("�", str(msg.content or "")) if msg.content else None

        tool_calls: list[dict] = []
        for tc in msg.tool_calls or []:
            # arguments 是 JSON 字符串，解析失败时回退为空 dict
            try:
                raw_args = _SURROGATE_RE.sub("�", str(tc.function.arguments or "{}"))
                arguments = json.loads(raw_args)
            except json.JSONDecodeError:
                arguments = {}
            tool_calls.append(
                {"id": tc.id, "name": tc.function.name, "arguments": arguments}
            )
        return {"content": content, "tool_calls": tool_calls}

