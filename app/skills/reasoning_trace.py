"""Install provider-visible reasoning capture for every Agenelf runtime.

推理状态与终端渲染由 ``core.reasoning_trace.install_reasoning_trace`` 幂等
安装；``llm.chat`` 本体保持未包装——轨迹捕获通过 ``Agent.add_llm_wrapper``
注册为显式有序钩子（priority=100，位于传输恢复层之内），不再依赖技能
文件名加载顺序。
"""
from __future__ import annotations

from typing import Any

from core.reasoning_trace import _chat_with_trace, install_reasoning_trace

SKILL_META = {
    "name": "reasoning_trace",
    "description": (
        "捕获 OpenAI 兼容接口返回的 reasoning_content，在交互终端以独立样式实时展示，"
        "并在工具调用链中按协议回传推理内容。"
    ),
    "version": "1.0.0",
}

CAPABILITY_META = {
    "id": "agent.reasoning_trace",
    "name": "可见推理轨迹",
    "description": (
        "只展示模型供应商明确返回的 reasoning_content；不伪造思考文本，不写入长期记忆，"
        "终端输出使用独立的青色、斜体、弱化样式与最终答案区分。"
    ),
    "version": "1.0.0",
    "domain": "observability",
    "operations": [],
    "composes_with": [
        "agent.workflow",
        "agent.task_continuation",
        "docker.operations",
    ],
}

TOOLS: list[dict[str, Any]] = []


def configure_runtime(
    *,
    agent: Any,
    config: dict[str, Any] | None = None,
    **_: Any,
) -> None:
    llm = getattr(agent, "llm", None)
    if llm is None:
        return
    full = config if isinstance(config, dict) else getattr(agent, "config", {})
    # 幂等安装推理状态、监听器与 close_reasoning_display 等辅助方法。
    install_reasoning_trace(llm, full)
    # 还原未被包装的 llm.chat：轨迹捕获改由 Agent 的有序钩子管线应用，
    # 避免与传输恢复等其它包装形成隐式的文件名排序依赖。
    original = getattr(llm, "_agenelf_original_chat", None)
    if original is not None:
        llm.chat = original
    add_wrapper = getattr(agent, "add_llm_wrapper", None)
    if not callable(add_wrapper):
        return

    def reasoning_wrapper(call_next: Any, messages: list[dict], tools: Any = None) -> dict:
        return _chat_with_trace(llm, call_next, messages, tools)

    add_wrapper(reasoning_wrapper, priority=100, name="reasoning_trace")


def execute(tool_name: str, args: dict[str, Any]) -> str:
    del tool_name, args
    return "reasoning_trace 是运行时可观测能力，不暴露模型可直接调用的工具。"
