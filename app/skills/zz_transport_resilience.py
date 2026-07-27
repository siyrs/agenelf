"""Last-stage model transport resilience for every Agenelf runtime.

The reasoning UI is observability, not a reason to terminate the CLI.  If a provider
closes an HTTP chunked stream early, this wrapper retries the same model turn with
streaming disabled.  Bounded retry applies only to connection, timeout, incomplete
stream and retryable server errors; authentication and malformed requests still fail
immediately.

包装器通过 ``Agent.add_llm_wrapper`` 注册为显式有序钩子：priority=1000 使其
成为**最外层**包装器（最先看到请求、最后处理内层异常），保留旧的“zz_ 最后
加载=最外层”语义，但不再依赖技能文件名排序；同名注册覆盖保证幂等。文件名
保持不变仅为兼容现有测试与文档引用。
"""
from __future__ import annotations

import os
import time
from typing import Any

SKILL_META = {
    "name": "zz_transport_resilience",
    "description": (
        "对 reasoning 流中断、连接重置和超时执行有界重试，并自动回退非流式响应；"
        "最终失败由任务续跑层保存检查点，不再退出 CLI。"
    ),
    "version": "1.0.0",
}

CAPABILITY_META = {
    "id": "agent.transport_resilience",
    "name": "模型传输恢复",
    "description": "流式读取失败时有界回退，不重复执行已返回的工具调用。",
    "version": "1.0.0",
    "domain": "reliability",
    "operations": [],
    "composes_with": ["agent.reasoning_trace", "agent.tool_budget_continuation"],
}

TOOLS: list[dict[str, Any]] = []

_TRANSIENT_NAMES = {
    "RemoteProtocolError",
    "ReadError",
    "WriteError",
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
    "WriteTimeout",
    "PoolTimeout",
    "TimeoutException",
    "APIConnectionError",
    "APITimeoutError",
    "InternalServerError",
    "RateLimitError",
}
_TRANSIENT_TEXT = (
    "incomplete chunked read",
    "peer closed connection",
    "connection reset",
    "connection aborted",
    "server disconnected",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway timeout",
)


def is_transient_transport_error(exc: BaseException) -> bool:
    name = type(exc).__name__
    module = type(exc).__module__.lower()
    text = str(exc).lower()
    if name in _TRANSIENT_NAMES:
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and (status == 429 or 500 <= status <= 599):
        return True
    if any(token in text for token in _TRANSIENT_TEXT):
        return True
    return module.startswith(("httpx", "httpcore", "openai")) and any(
        token in name.lower() for token in ("timeout", "connection", "protocol", "server")
    )


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(number, maximum))


def _bounded_float(value: object, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(number, maximum))


def configure_runtime(*, agent: Any, config: dict[str, Any] | None = None, **_: Any) -> None:
    llm = getattr(agent, "llm", None)
    if llm is None or not callable(getattr(llm, "chat", None)):
        return

    full = config if isinstance(config, dict) else getattr(agent, "config", {})
    llm_cfg = full.get("llm", {}) if isinstance(full, dict) else {}
    if not isinstance(llm_cfg, dict):
        llm_cfg = {}
    retries = _bounded_int(
        os.environ.get(
            "AGENELF_TRANSPORT_RETRY_ATTEMPTS",
            llm_cfg.get("transport_retry_attempts", 2),
        ),
        2,
        0,
        4,
    )
    backoff = _bounded_float(
        os.environ.get(
            "AGENELF_TRANSPORT_RETRY_BACKOFF_SECONDS",
            llm_cfg.get("transport_retry_backoff_seconds", 0.4),
        ),
        0.4,
        0.0,
        5.0,
    )
    fallback = str(
        os.environ.get(
            "AGENELF_STREAM_FALLBACK_NON_STREAM",
            llm_cfg.get("stream_fallback_non_stream", True),
        )
    ).strip().lower() not in {"0", "false", "off", "no"}

    add_wrapper = getattr(agent, "add_llm_wrapper", None)
    if not callable(add_wrapper):
        return

    def transport_wrapper(
        call_next: Any,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        original_stream = getattr(llm, "_agenelf_stream_reasoning", None)
        last_error: BaseException | None = None
        try:
            for attempt in range(retries + 1):
                try:
                    return call_next(messages, tools=tools)
                except Exception as exc:
                    last_error = exc
                    if not is_transient_transport_error(exc) or attempt >= retries:
                        raise
                    # A partial stream never returned a completed tool call to the Agent,
                    # so retrying this model turn is safe. Prefer non-stream for recovery.
                    if fallback and original_stream is not None:
                        llm._agenelf_stream_reasoning = False
                    if backoff:
                        time.sleep(backoff * (attempt + 1))
            assert last_error is not None
            raise last_error
        finally:
            if original_stream is not None:
                llm._agenelf_stream_reasoning = original_stream

    # 最外层（priority 最大）：包裹推理轨迹等所有内层包装器。
    add_wrapper(transport_wrapper, priority=1000, name="zz_transport_resilience")


def execute(tool_name: str, args: dict[str, Any]) -> str:
    del tool_name, args
    return "zz_transport_resilience 是运行时可靠性能力，不暴露模型工具。"
