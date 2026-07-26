"""Provider-visible reasoning capture, round-trip, and terminal rendering.

Agenelf treats ``reasoning_content`` as provider output, not as a fabricated status
message. The wrapper keeps the existing OpenAI-compatible ``LLMClient`` surface,
streams reasoning when a terminal listener is attached, and preserves DeepSeek's
required reasoning round-trip for assistant messages that contain tool calls.
"""
from __future__ import annotations

import json
import os
import re
import sys
from types import MethodType
from typing import Any, Callable

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from core.privacy import redact_sensitive_text

ReasoningListener = Callable[[dict[str, Any]], None]

_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
_PROXY_URI_RE = re.compile(
    r"(?i)\b(vmess|vless|trojan|ss|ssr|hysteria2?|tuic)://[^\s\"']+"
)
_URL_SECRET_RE = re.compile(
    r"(?i)([?&](?:token|secret|password|passwd|api[_-]?key|key)=)[^&\s\"']+"
)
_DEFAULT_MAX_CHARS = 60_000
_MAX_REASONING_CACHE = 128


def _truthy(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {
        "",
        "0",
        "false",
        "off",
        "no",
        "disabled",
    }


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    direct = getattr(value, name, None)
    if direct is not None:
        return direct
    extra = getattr(value, "model_extra", None)
    if isinstance(extra, dict) and name in extra:
        return extra[name]
    return default


def _reasoning_piece(value: Any) -> str:
    for name in ("reasoning_content", "reasoning", "thinking"):
        candidate = _field(value, name)
        if isinstance(candidate, str) and candidate:
            return _SURROGATE_RE.sub("�", candidate)
    return ""


def _clean_text(value: object) -> str:
    return _SURROGATE_RE.sub("�", str(value or ""))


def _sanitize_display(value: object) -> str:
    text = redact_sensitive_text(_clean_text(value))
    text = _PROXY_URI_RE.sub(
        lambda match: f"{match.group(1)}://[REDACTED]", text
    )
    return _URL_SECRET_RE.sub(r"\1[REDACTED]", text)


def _sanitize_surrogates(value: Any) -> Any:
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, dict):
        return {
            key: _sanitize_surrogates(item) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_surrogates(item) for item in value]
    return value


def _tool_call_id(tool_call: Any) -> str:
    return str(_field(tool_call, "id", "") or "")


def _tool_call_name(tool_call: Any) -> str:
    function = _field(tool_call, "function", {})
    return str(_field(function, "name", "") or "")


def _tool_call_arguments(tool_call: Any) -> str:
    function = _field(tool_call, "function", {})
    return str(_field(function, "arguments", "{}") or "{}")


def _parse_arguments(raw: object) -> dict[str, Any]:
    try:
        value = json.loads(_clean_text(raw) or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _normalize_tool_calls(raw_calls: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, tool_call in enumerate(raw_calls or []):
        result.append(
            {
                "id": _tool_call_id(tool_call) or f"call-{index}",
                "name": _tool_call_name(tool_call),
                "arguments": _parse_arguments(
                    _tool_call_arguments(tool_call)
                ),
            }
        )
    return result


def _append_stream_tool_call(
    fragments: dict[int, dict[str, str]],
    tool_call: Any,
    fallback_index: int,
) -> None:
    try:
        index = int(_field(tool_call, "index", fallback_index))
    except (TypeError, ValueError):
        index = fallback_index
    current = fragments.setdefault(
        index, {"id": "", "name": "", "arguments": ""}
    )
    call_id = _tool_call_id(tool_call)
    if call_id:
        current["id"] = call_id
    name = _tool_call_name(tool_call)
    if name:
        current["name"] += name
    arguments = _tool_call_arguments(tool_call)
    if arguments and arguments != "{}":
        current["arguments"] += arguments


def _finish_stream_tool_calls(
    fragments: dict[int, dict[str, str]],
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for index, fragment in sorted(fragments.items()):
        calls.append(
            {
                "id": fragment["id"] or f"call-{index}",
                "name": fragment["name"],
                "arguments": _parse_arguments(
                    fragment["arguments"] or "{}"
                ),
            }
        )
    return calls


def _emit(llm: Any, event: dict[str, Any]) -> None:
    listener = getattr(llm, "_agenelf_reasoning_listener", None)
    if not callable(listener):
        return
    try:
        listener(dict(event))
    except Exception:
        # UI/observability must never break a model or tool request.
        return


def _cache_reasoning(
    llm: Any,
    tool_calls: list[dict[str, Any]],
    reasoning: str,
) -> None:
    if not reasoning or not tool_calls:
        return
    cache = getattr(llm, "_agenelf_reasoning_by_tool_call", None)
    if not isinstance(cache, dict):
        cache = {}
        llm._agenelf_reasoning_by_tool_call = cache
    for tool_call in tool_calls:
        call_id = str(tool_call.get("id", ""))
        if call_id:
            cache[call_id] = reasoning
    while len(cache) > _MAX_REASONING_CACHE:
        cache.pop(next(iter(cache)))


def _inject_reasoning_for_tool_turns(
    llm: Any,
    messages: list[dict[str, Any]],
) -> None:
    cache = getattr(llm, "_agenelf_reasoning_by_tool_call", {})
    if not isinstance(cache, dict) or not cache:
        return
    for message in messages:
        if (
            not isinstance(message, dict)
            or message.get("role") != "assistant"
            or message.get("reasoning_content")
        ):
            continue
        for tool_call in message.get("tool_calls") or []:
            reasoning = cache.get(_tool_call_id(tool_call))
            if reasoning:
                message["reasoning_content"] = reasoning
                break


def _request_kwargs(
    llm: Any,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": getattr(llm, "model"),
        "messages": messages,
        "temperature": getattr(llm, "temperature", 0.6),
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"

    cfg = getattr(llm, "_agenelf_reasoning_llm_config", {})
    if isinstance(cfg, dict):
        if cfg.get("reasoning_effort"):
            kwargs["reasoning_effort"] = cfg["reasoning_effort"]
        thinking = cfg.get("thinking")
        if thinking is not None:
            if isinstance(thinking, dict):
                thinking_body = dict(thinking)
            elif isinstance(thinking, bool):
                thinking_body = {
                    "type": "enabled" if thinking else "disabled"
                }
            else:
                thinking_body = {"type": str(thinking)}
            kwargs["extra_body"] = {"thinking": thinking_body}
        if cfg.get("max_tokens") is not None:
            kwargs["max_tokens"] = int(cfg["max_tokens"])
    return _sanitize_surrogates(kwargs)


def _complete_event(
    llm: Any,
    round_number: int,
    reasoning: str,
    tool_calls: list[dict[str, Any]],
) -> None:
    _emit(
        llm,
        {
            "type": "reasoning_completed",
            "round": round_number,
            "model": str(getattr(llm, "model", "")),
            "available": bool(reasoning),
            "text": _sanitize_display(reasoning),
            "tool_call_count": len(tool_calls),
        },
    )


def _non_stream_chat(
    llm: Any,
    kwargs: dict[str, Any],
    round_number: int,
) -> dict[str, Any]:
    response = llm._client.chat.completions.create(**kwargs)
    choices = _field(response, "choices", []) or []
    if not choices:
        raise RuntimeError("模型响应没有 choices")
    message = _field(choices[0], "message")
    reasoning = _reasoning_piece(message)
    content_raw = _field(message, "content")
    content = _clean_text(content_raw) if content_raw else None
    tool_calls = _normalize_tool_calls(
        _field(message, "tool_calls", []) or []
    )
    if reasoning:
        safe_reasoning = _sanitize_display(reasoning)
        _emit(
            llm,
            {
                "type": "reasoning_delta",
                "round": round_number,
                "delta": safe_reasoning,
                "text": safe_reasoning,
            },
        )
    _cache_reasoning(llm, tool_calls, reasoning)
    _complete_event(llm, round_number, reasoning, tool_calls)
    return {
        "content": content,
        "reasoning_content": reasoning or None,
        "tool_calls": tool_calls,
    }


def _stream_chat(
    llm: Any,
    kwargs: dict[str, Any],
    round_number: int,
) -> dict[str, Any]:
    stream = llm._client.chat.completions.create(stream=True, **kwargs)
    reasoning = ""
    content = ""
    fragments: dict[int, dict[str, str]] = {}
    for chunk in stream:
        choices = _field(chunk, "choices", []) or []
        if not choices:
            continue
        delta = _field(choices[0], "delta")
        if delta is None:
            continue
        piece = _reasoning_piece(delta)
        if piece:
            reasoning += piece
            _emit(
                llm,
                {
                    "type": "reasoning_delta",
                    "round": round_number,
                    "delta": _sanitize_display(piece),
                    "text": _sanitize_display(reasoning),
                },
            )
        content_piece = _field(delta, "content")
        if isinstance(content_piece, str) and content_piece:
            content += _clean_text(content_piece)
        for fallback_index, tool_call in enumerate(
            _field(delta, "tool_calls", []) or []
        ):
            _append_stream_tool_call(
                fragments, tool_call, fallback_index
            )

    tool_calls = _finish_stream_tool_calls(fragments)
    _cache_reasoning(llm, tool_calls, reasoning)
    _complete_event(llm, round_number, reasoning, tool_calls)
    return {
        "content": content or None,
        "reasoning_content": reasoning or None,
        "tool_calls": tool_calls,
    }


def _chat_with_trace(
    llm: Any,
    original_chat: Callable[..., dict[str, Any]],
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    _inject_reasoning_for_tool_turns(llm, messages)
    llm._agenelf_reasoning_round = int(
        getattr(llm, "_agenelf_reasoning_round", 0)
    ) + 1
    round_number = llm._agenelf_reasoning_round
    _emit(
        llm,
        {
            "type": "reasoning_started",
            "round": round_number,
            "model": str(getattr(llm, "model", "")),
        },
    )

    client = getattr(llm, "_client", None)
    if client is None:
        response = original_chat(messages, tools=tools)
        if not isinstance(response, dict):
            raise TypeError("LLM chat 必须返回 dict")
        reasoning = _clean_text(
            response.get("reasoning_content", "")
        )
        tool_calls = response.get("tool_calls") or []
        if reasoning:
            safe_reasoning = _sanitize_display(reasoning)
            _emit(
                llm,
                {
                    "type": "reasoning_delta",
                    "round": round_number,
                    "delta": safe_reasoning,
                    "text": safe_reasoning,
                },
            )
        _cache_reasoning(llm, tool_calls, reasoning)
        _complete_event(llm, round_number, reasoning, tool_calls)
        return response

    kwargs = _request_kwargs(llm, messages, tools)
    listener = getattr(llm, "_agenelf_reasoning_listener", None)
    use_stream = bool(listener) and bool(
        getattr(llm, "_agenelf_stream_reasoning", True)
    )
    try:
        if use_stream:
            try:
                return _stream_chat(llm, kwargs, round_number)
            except (TypeError, NotImplementedError, AttributeError) as exc:
                _emit(
                    llm,
                    {
                        "type": "reasoning_stream_fallback",
                        "round": round_number,
                        "message": _sanitize_display(exc),
                    },
                )
        return _non_stream_chat(llm, kwargs, round_number)
    except BaseException as exc:
        _emit(
            llm,
            {
                "type": "reasoning_failed",
                "round": round_number,
                "message": _sanitize_display(
                    f"{type(exc).__name__}: {exc}"
                ),
            },
        )
        raise


class ReasoningPanelRenderer:
    """Render reasoning in a persistent cyan/dim/italic terminal panel."""

    def __init__(
        self,
        console: Console | None = None,
        *,
        max_chars: int = _DEFAULT_MAX_CHARS,
    ) -> None:
        self.console = console or Console()
        self.max_chars = max(1_000, min(int(max_chars), 500_000))
        self._live: Live | None = None
        self._round = 0
        self._text = ""

    def _bounded(self, text: str) -> str:
        if len(text) <= self.max_chars:
            return text
        return (
            f"…（思考内容过长，仅显示最近 {self.max_chars} 个字符）\n"
            + text[-self.max_chars :]
        )

    def panel(self, text: str | None = None) -> Panel:
        body = Text(
            self._bounded(text if text is not None else self._text),
            style="italic dim bright_cyan",
        )
        return Panel(
            body,
            title=(
                "🧠 Agenelf 思考过程 · "
                f"第 {max(1, self._round)} 轮"
            ),
            title_align="left",
            subtitle="reasoning_content · 实时",
            subtitle_align="right",
            border_style="cyan",
            padding=(0, 1),
        )

    def _start(self) -> None:
        if self._live is not None:
            return
        self._live = Live(
            self.panel(),
            console=self.console,
            refresh_per_second=10,
            transient=False,
            vertical_overflow="visible",
        )
        self._live.start()

    def _stop(self) -> None:
        if self._live is None:
            return
        self._live.stop()
        self._live = None

    def handle(self, event: dict[str, Any]) -> None:
        kind = str(event.get("type", ""))
        if kind == "reasoning_started":
            self._stop()
            self._round = int(
                event.get("round", self._round + 1) or 1
            )
            self._text = ""
            return
        if kind == "reasoning_delta":
            self._text = str(event.get("text", self._text))
            if not self._text:
                return
            self._start()
            assert self._live is not None
            self._live.update(self.panel(), refresh=True)
            return
        if kind == "reasoning_completed":
            text = str(event.get("text", self._text))
            if text:
                self._text = text
                self._start()
                assert self._live is not None
                self._live.update(self.panel(), refresh=True)
            self._stop()
            return
        if kind == "reasoning_failed":
            self._text = "推理流读取失败：" + str(
                event.get("message", "未知错误")
            )
            self._start()
            assert self._live is not None
            self._live.update(self.panel(), refresh=True)
            self._stop()

    def close(self) -> None:
        self._stop()


def _automatic_renderer(
    config: dict[str, Any] | None,
) -> ReasoningPanelRenderer | None:
    config = config if isinstance(config, dict) else {}
    raw_cli_cfg = config.get("cli", {})
    cli_cfg = raw_cli_cfg if isinstance(raw_cli_cfg, dict) else {}
    env_value = os.environ.get("AGENELF_SHOW_REASONING")
    enabled = _truthy(
        env_value,
        _truthy(cli_cfg.get("show_reasoning"), True),
    )
    force = _truthy(
        os.environ.get("AGENELF_FORCE_REASONING_DISPLAY"),
        False,
    )
    if not enabled or (not force and not sys.stdout.isatty()):
        return None
    raw_max = os.environ.get(
        "AGENELF_REASONING_MAX_CHARS",
        str(
            cli_cfg.get(
                "reasoning_max_chars", _DEFAULT_MAX_CHARS
            )
        ),
    )
    try:
        max_chars = int(raw_max)
    except (TypeError, ValueError):
        max_chars = _DEFAULT_MAX_CHARS
    return ReasoningPanelRenderer(max_chars=max_chars)


def install_reasoning_trace(
    llm: Any,
    config: dict[str, Any] | None = None,
    *,
    listener: ReasoningListener | None = None,
) -> Any:
    """Install an idempotent reasoning wrapper on an LLM client."""

    if getattr(llm, "_agenelf_reasoning_trace_installed", False):
        if listener is not None:
            llm._agenelf_reasoning_listener = listener
        return llm

    full_config = config if isinstance(config, dict) else {}
    llm_cfg = full_config.get("llm", full_config)
    if not isinstance(llm_cfg, dict):
        llm_cfg = {}
    llm._agenelf_reasoning_llm_config = dict(llm_cfg)
    llm._agenelf_stream_reasoning = _truthy(
        llm_cfg.get("stream_reasoning"), True
    )
    llm._agenelf_reasoning_by_tool_call = {}
    llm._agenelf_reasoning_round = 0
    llm._agenelf_reasoning_trace_installed = True

    renderer = None
    if listener is None:
        renderer = _automatic_renderer(full_config)
        if renderer is not None:
            listener = renderer.handle
    llm._agenelf_reasoning_renderer = renderer
    llm._agenelf_reasoning_listener = listener

    original_chat = llm.chat
    llm._agenelf_original_chat = original_chat

    def set_reasoning_listener(
        self: Any,
        value: ReasoningListener | None,
    ) -> None:
        self._agenelf_reasoning_listener = value

    def close_reasoning_display(self: Any) -> None:
        active = getattr(
            self, "_agenelf_reasoning_renderer", None
        )
        if active is not None:
            active.close()

    def chat_with_reasoning(
        self: Any,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return _chat_with_trace(
            self, original_chat, messages, tools
        )

    llm.set_reasoning_listener = MethodType(
        set_reasoning_listener, llm
    )
    llm.close_reasoning_display = MethodType(
        close_reasoning_display, llm
    )
    llm.chat = MethodType(chat_with_reasoning, llm)
    return llm
