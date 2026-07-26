"""Install provider-visible reasoning capture for every Agenelf runtime."""
from __future__ import annotations

from typing import Any

from core.reasoning_trace import install_reasoning_trace

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
    install_reasoning_trace(
        agent.llm,
        config or getattr(agent, "config", {}),
    )


def execute(tool_name: str, args: dict[str, Any]) -> str:
    del tool_name, args
    return "reasoning_trace 是运行时可观测能力，不暴露模型可直接调用的工具。"
