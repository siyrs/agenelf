"""ai_tools 技能：通用 AI 能力（嵌套调用 LLM）。

LLM 不在技能内直接实现，而是通过 ``set_llm`` 注入一个 callable::

    fn(messages: list[dict]) -> str

未注入时所有工具返回 mock 提示，保证技能可离线加载与测试。
"""

from __future__ import annotations

from typing import Callable

SKILL_META = {
    "name": "ai_tools",
    "description": "通用 AI 能力：向 LLM 提问、总结文本。LLM 通过 set_llm 注入，未配置时处于 mock 模式。",
    "version": "0.1.0",
}

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "ask_llm",
            "description": "通用 AI 能力：向大语言模型发送一段提示词并返回其回答，可选 system 角色设定。",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "发送给 LLM 的用户提示词。",
                    },
                    "system": {
                        "type": "string",
                        "description": "可选的系统提示词（角色/风格设定），默认为空。",
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "summarize",
            "description": "通用 AI 能力：把一段长文本交给 LLM 提炼为简洁的中文摘要。",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "需要总结的原文。",
                    },
                },
                "required": ["text"],
            },
        },
    },
]

# 注入的 LLM 调用器，签名：fn(messages: list[dict]) -> str
_llm_callable: Callable[[list[dict]], str] | None = None


def set_llm(fn: Callable[[list[dict]], str] | None) -> None:
    """注入（或清除）LLM 调用器。"""
    global _llm_callable
    _llm_callable = fn


def _chat(messages: list[dict]) -> str:
    """统一的 LLM 调用入口，未配置时返回 mock 提示。"""
    if _llm_callable is None:
        return "LLM 未配置（当前为 mock 模式）"
    try:
        result = _llm_callable(messages)
    except Exception as exc:
        return f"LLM 调用失败：{type(exc).__name__}: {exc}"
    return result if isinstance(result, str) else str(result)


def ask_llm(prompt: str, system: str = "") -> str:
    """向 LLM 提问，可选 system 提示词。"""
    if not isinstance(prompt, str) or not prompt.strip():
        return "调用失败：prompt 不能为空"
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return _chat(messages)


def summarize(text: str) -> str:
    """让 LLM 用简洁中文总结一段文本。"""
    if not isinstance(text, str) or not text.strip():
        return "调用失败：text 不能为空"
    prompt = f"请用简洁的中文总结以下内容，保留关键信息：\n\n{text}"
    return _chat([{"role": "user", "content": prompt}])


_DISPATCH = {
    "ask_llm": lambda a: ask_llm(a.get("prompt", ""), a.get("system", "")),
    "summarize": lambda a: summarize(a.get("text", "")),
}


def execute(tool_name: str, args: dict) -> str:
    """按协议路由工具调用；内部捕获所有异常并返回字符串。"""
    handler = _DISPATCH.get(tool_name)
    if handler is None:
        return f"未知工具：{tool_name}，可用工具：{', '.join(sorted(_DISPATCH))}"
    try:
        return handler(args or {})
    except Exception as exc:  # 兜底：协议要求永不抛异常
        return f"执行失败：{type(exc).__name__}: {exc}"
