"""Interactive slash-command palette for the Agenelf terminal.

The command catalogue is the single source for the startup hint, help table and
prompt-toolkit completion menu.  Typing ``/`` opens the menu; arrow keys use
prompt-toolkit's native selection handling and Tab accepts the selected item.
"""
from __future__ import annotations

import difflib
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from prompt_toolkit import PromptSession
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.shortcuts import CompleteStyle
from prompt_toolkit.styles import Style
from rich.console import Console


@dataclass(frozen=True)
class SlashCommand:
    name: str
    description: str
    usage: str = ""
    aliases: tuple[str, ...] = ()


COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand("/help", "显示全部命令、用途和参数", aliases=("/commands",)),
    SlashCommand("/doctor", "检查运行时、Runner、队列、挂载与技能健康"),
    SlashCommand("/self", "查看可观测自我模型"),
    SlashCommand("/assess", "评估当前能力与缺口"),
    SlashCommand("/scorecard", "查看可信能力健康评分"),
    SlashCommand("/roadmap", "查看证据驱动改进路线图"),
    SlashCommand("/mind", "查看持续成长状态"),
    SlashCommand("/reflect", "执行反思与沉淀", "/reflect [--deep]"),
    SlashCommand("/intentions", "列出改进意向", "/intentions [status]"),
    SlashCommand("/intend", "创建改进意向", "/intend [P0|P1|P2|P3] <目标>"),
    SlashCommand("/pursue", "推进指定改进意向", "/pursue <intent-id> [--apply]"),
    SlashCommand("/validate", "运行软件验证", "/validate [check|suite|result] ..."),
    SlashCommand("/autonomy", "运行受控自主改进", "/autonomy [--plan-only] [目标]"),
    SlashCommand("/local", "查看本地个性化配置状态"),
    SlashCommand("/local-reload", "重新加载本地上下文"),
    SlashCommand("/remember", "记录主人事实或偏好", "/remember <fact|preference> <内容>"),
    SlashCommand("/recall", "检索主人记忆", "/recall <关键词>"),
    SlashCommand("/ops", "查看运维请求或指定请求结果", "/ops [op-id]"),
    SlashCommand("/approvals", "列出等待主人审批的请求"),
    SlashCommand("/approve", "批准精确绑定的请求", "/approve [op-id]"),
    SlashCommand("/deny", "拒绝精确绑定的请求", "/deny [op-id] [原因]"),
    SlashCommand("/reload", "重载指定技能", "/reload <技能名>"),
    SlashCommand("/newskill", "生成新技能候选", "/newskill <描述>"),
    SlashCommand("/memory", "查看长期记忆摘要"),
    SlashCommand("/evolve", "执行受控自主迭代", "/evolve <目标>"),
    SlashCommand("/skills", "列出已加载技能"),
    SlashCommand("/capabilities", "列出能力域与操作风险"),
    SlashCommand("/quit", "退出交互终端", aliases=("/exit",)),
)

_COMMAND_BY_NAME: dict[str, SlashCommand] = {}
for _command in COMMANDS:
    _COMMAND_BY_NAME[_command.name] = _command
    for _alias in _command.aliases:
        _COMMAND_BY_NAME[_alias] = _command


def command_names(*, include_aliases: bool = False) -> list[str]:
    names = [item.name for item in COMMANDS]
    if include_aliases:
        names.extend(alias for item in COMMANDS for alias in item.aliases)
    return names


def canonical_command(value: str) -> str:
    raw = str(value or "").strip().lower()
    spec = _COMMAND_BY_NAME.get(raw)
    return spec.name if spec else raw


def command_spec(value: str) -> SlashCommand | None:
    return _COMMAND_BY_NAME.get(str(value or "").strip().lower())


def close_command_matches(value: str, limit: int = 3) -> list[str]:
    raw = str(value or "").strip().lower()
    return difflib.get_close_matches(raw, command_names(include_aliases=True), n=limit, cutoff=0.45)


def command_hint() -> str:
    return "命令：输入 [bold cyan]/[/bold cyan] 打开菜单；[cyan]↑↓[/cyan] 选择，Tab 补全，Enter 执行；/help 查看完整清单"


def command_rows() -> list[tuple[str, str, str]]:
    return [
        (
            item.name + ("（" + "、".join(item.aliases) + "）" if item.aliases else ""),
            item.usage or item.name,
            item.description,
        )
        for item in COMMANDS
    ]


def _truthy(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"", "0", "false", "off", "no", "disabled"}


def _value_fragment(text: str) -> tuple[list[str], str]:
    if not text:
        return [], ""
    parts = text.split()
    if text[-1:].isspace():
        return parts, ""
    return parts[:-1], parts[-1] if parts else ""


class SlashCommandCompleter(Completer):
    """Complete command names and safe, read-only argument candidates."""

    def __init__(self, agent: Any | None = None) -> None:
        self.agent = agent

    @staticmethod
    def _pending_operations() -> list[tuple[str, str]]:
        try:
            from core import owner_approval

            return [
                (
                    str(item.get("id", "")),
                    f"{item.get('operation', '')} · {item.get('target', '')} · {str(item.get('summary', ''))[:80]}",
                )
                for item in owner_approval.list_pending_operations()
                if item.get("id")
            ]
        except Exception:
            return []

    @staticmethod
    def _recent_operations() -> list[tuple[str, str]]:
        try:
            from core import operations

            paths = operations.queue_paths()
            request_paths = sorted(
                paths["requests"].glob("op-*.json"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )[:30]
            values: list[tuple[str, str]] = []
            for path in request_paths:
                state = operations.get_operation(path.stem)
                request = state.get("request", {}) if isinstance(state, dict) else {}
                values.append(
                    (
                        path.stem,
                        f"{state.get('status', '')} · {request.get('operation', '')} · {request.get('target', '')}",
                    )
                )
            return values
        except Exception:
            return []

    def _skill_names(self) -> list[tuple[str, str]]:
        registry = getattr(self.agent, "registry", None)
        skills = getattr(registry, "skills", {})
        if not isinstance(skills, dict):
            return []
        values: list[tuple[str, str]] = []
        for name, module in sorted(skills.items()):
            meta = getattr(module, "SKILL_META", {})
            description = str(meta.get("description", "")) if isinstance(meta, dict) else ""
            values.append((str(name), description[:100]))
        return values

    def _argument_options(
        self, command: str, prior: list[str]
    ) -> list[tuple[str, str]]:
        if command in {"/approve", "/deny"}:
            return self._pending_operations()
        if command == "/ops":
            return self._recent_operations()
        if command == "/reload":
            return self._skill_names()
        if command == "/reflect" and not prior:
            return [("--deep", "执行深度反思")]
        if command == "/autonomy" and not prior:
            return [("--plan-only", "只生成计划，不应用改动")]
        if command == "/pursue" and prior and "--apply" not in prior:
            return [("--apply", "在受控沙盒中应用改动")]
        if command == "/remember" and not prior:
            return [("fact", "记录事实"), ("preference", "记录偏好")]
        if command == "/intend" and not prior:
            return [(value, "意向优先级") for value in ("P0", "P1", "P2", "P3")]
        if command == "/validate" and not prior:
            return [
                ("check", "运行单个检查"),
                ("suite", "运行验证套件"),
                ("result", "查询验证结果"),
            ]
        return []

    def get_completions(self, document: Document, complete_event: Any) -> Iterable[Completion]:
        del complete_event
        text = document.text_before_cursor
        if not text.startswith("/"):
            return

        head, separator, tail = text.partition(" ")
        if not separator:
            fragment = head.lower()
            seen: set[str] = set()
            for item in COMMANDS:
                candidates = (item.name, *item.aliases)
                if not any(candidate.startswith(fragment) for candidate in candidates):
                    continue
                if item.name in seen:
                    continue
                seen.add(item.name)
                alias_text = f" · 别名 {'、'.join(item.aliases)}" if item.aliases else ""
                yield Completion(
                    item.name,
                    start_position=-len(head),
                    display=item.name,
                    display_meta=item.description + alias_text,
                )
            return

        command = canonical_command(head)
        prior, fragment = _value_fragment(tail)
        used = set(prior)
        for value, meta in self._argument_options(command, prior):
            if not value or value in used or not value.lower().startswith(fragment.lower()):
                continue
            yield Completion(
                value,
                start_position=-len(fragment),
                display=value,
                display_meta=meta,
            )


def accept_selected_completion(buffer: Buffer) -> bool:
    """Apply the highlighted completion; otherwise open the menu on its first row."""

    state = buffer.complete_state
    if state is not None and state.current_completion is not None:
        buffer.apply_completion(state.current_completion)
        return True
    buffer.start_completion(select_first=True)
    return False


def command_key_bindings() -> KeyBindings:
    bindings = KeyBindings()

    @bindings.add("tab")
    def _accept_or_open(event: Any) -> None:
        accept_selected_completion(event.current_buffer)

    @bindings.add("s-tab")
    def _previous(event: Any) -> None:
        buffer = event.current_buffer
        if buffer.complete_state is None:
            buffer.start_completion(select_first=True)
        else:
            buffer.complete_previous()

    return bindings


class InteractivePrompt:
    """TTY-aware PromptSession with a Rich-compatible fallback."""

    def __init__(
        self,
        *,
        agent: Any,
        console: Console,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.agent = agent
        self.console = console
        self.config = config if isinstance(config, dict) else {}
        self.session: PromptSession[str] | None = None

        cli_raw = self.config.get("cli", {})
        cli = cli_raw if isinstance(cli_raw, dict) else {}
        enabled = _truthy(
            os.environ.get("AGENELF_INTERACTIVE_COMPLETION"),
            _truthy(cli.get("interactive_completion"), True),
        )
        force = _truthy(os.environ.get("AGENELF_FORCE_INTERACTIVE_PROMPT"), False)
        terminal = sys.stdin.isatty() and sys.stdout.isatty()
        if not enabled or (not force and not terminal):
            return

        try:
            menu_rows = max(4, min(int(cli.get("command_menu_rows", 12)), 30))
        except (TypeError, ValueError):
            menu_rows = 12

        style = Style.from_dict(
            {
                "prompt": "bold ansiblue",
                "bottom-toolbar": "bg:#202020 #bcbcbc",
                "completion-menu.completion": "bg:#202020 #d0d0d0",
                "completion-menu.completion.current": "bg:#005f87 #ffffff bold",
                "completion-menu.meta.completion": "bg:#202020 #8a8a8a italic",
                "completion-menu.meta.completion.current": "bg:#005f87 #d7ffff italic",
                "scrollbar.background": "bg:#303030",
                "scrollbar.button": "bg:#5f5f5f",
            }
        )
        self.session = PromptSession(
            completer=SlashCommandCompleter(agent),
            complete_while_typing=True,
            complete_style=CompleteStyle.COLUMN,
            key_bindings=command_key_bindings(),
            history=InMemoryHistory(),
            enable_history_search=True,
            reserve_space_for_menu=menu_rows,
            complete_in_thread=False,
            mouse_support=False,
            style=style,
        )

    def read(self) -> str:
        if self.session is None:
            return self.console.input("[bold blue]你 > [/bold blue]")
        return self.session.prompt(
            HTML("<prompt>你 &gt; </prompt>"),
            bottom_toolbar=HTML(
                " 输入 <b>/</b> 查看命令 · ↑↓ 选择 · <b>Tab</b> 补全 · Enter 执行 · Ctrl+C 取消"
            ),
        )
