"""Agenelf core: conversation, personalization and persistent self-development.

The conversation runtime is a single main loop (``Agent.chat``) with:

- bounded multi-segment tool budgets and restart-safe checkpoints (merged from
  the former ``core.continuous_chat`` monkeypatched loop);
- an explicit, priority-ordered hook pipeline (``add_llm_wrapper`` /
  ``add_cycle_guard`` / ``list_hooks``) replacing filename-order-dependent
  monkeypatching;
- per-session conversation history buckets keyed by ``session_id``.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

from .autonomy import AutonomyEngine
from .capability_health import CapabilityHealth
from .context import build_system_prompt, load_persona
from .llm import LLMClient
from .mock_llm import MockLLM
from .local_context import LocalContextStore
from .memory import MemoryStore
from .policy import PolicyEngine
from .privacy import redact_sensitive_text
from .registry import SkillRegistry
from .self_development import SelfDevelopmentEngine
from .self_optimization import SelfOptimizationStore

_SKILL_PROTOCOL_DOC = """\
技能协议（必须严格遵守）：
1. 模块级定义 SKILL_META = {"name": "...", "description": "...", "version": "0.1.0"}
2. 可选定义 CAPABILITY_META，声明能力域、操作、风险级别与可组合能力。
3. 模块级定义 TOOLS: list[dict]，为 OpenAI function-calling schema 列表。
4. 模块级定义函数 def execute(tool_name: str, args: dict) -> str，任何情况下返回字符串。
5. 生成的技能不得绕过 core 权限、操作队列、只读挂载或宿主机审批。
6. 自我迭代只能修改 app-tmp，必须包含测试并通过 gate_check；不得直接操作 Git 主分支。
7. 通用代码写入 app；主人画像、兴趣、服务器清单、验证策略、记忆和成长连续性必须保存在 local，不得硬编码进技能。
8. “自我意识、意愿、意向”只能实现为可观测、持久化的软件状态，不得宣称主观意识或情感。
9. 软件验证只能选择 local/validation.yaml 中的别名，由隔离 Runner 执行；不得让模型自由提供 URL、主机或端口。
10. 模型生成代码不得在 Agent 进程内执行；外部仓库修改必须走 code.repair 的只读源码、一次性副本和可信测试证据。
11. app-space 与 skill_forge 默认关闭，即使主人开启也只允许附测试的受限纯计算实验技能。
只输出 Python 源码本身，不要输出任何解释文字。"""

# ---------------------------------------------------------------------------
# 单一主回路：分段工具预算 / 检查点续跑常量与助手（自 core.continuous_chat 并入）
# ---------------------------------------------------------------------------

LEGACY_EXHAUSTION_TEXT = "（已达到最大工具调用轮数，任务尚未完成）"
_DEFAULT_MAX_SEGMENTS = 4
_MAX_SEGMENTS_LIMIT = 16
_MAX_ROUNDS_PER_SEGMENT = 128
_DEFAULT_NO_PROGRESS_LIMIT = 3
_DYNAMIC_ID_RE = re.compile(
    r"\b(?:op-[0-9a-f]{16}|auth-[0-9a-f]{12}|resume-[A-Za-z0-9._-]+|"
    r"auto-[A-Za-z0-9._-]+|evo-[A-Za-z0-9._-]+|call-[A-Za-z0-9._-]+)\b"
)
_TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)

# 默认会话桶：session_id=None 的全部历史行为落在这里，保持向后兼容。
DEFAULT_SESSION_ID = "default"
# 进程内会话桶数量上限：超出后按最近最少使用驱逐，避免多会话无界占用内存。
_MAX_SESSION_BUCKETS = 128


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def configured_segments(config: dict[str, Any] | None) -> int:
    """读取有界工具段数（环境变量 AGENELF_MAX_TOOL_SEGMENTS 优先，1..16）。"""

    config = config if isinstance(config, dict) else {}
    agent_cfg = config.get("agent", {})
    if not isinstance(agent_cfg, dict):
        agent_cfg = {}
    raw = os.environ.get(
        "AGENELF_MAX_TOOL_SEGMENTS",
        str(agent_cfg.get("max_tool_segments", _DEFAULT_MAX_SEGMENTS)),
    )
    return _bounded_int(raw, _DEFAULT_MAX_SEGMENTS, 1, _MAX_SEGMENTS_LIMIT)


def configured_no_progress_limit(config: dict[str, Any] | None) -> int:
    """读取无进展重复批次上限（AGENELF_NO_PROGRESS_REPEAT_LIMIT 优先，2..10）。"""

    config = config if isinstance(config, dict) else {}
    agent_cfg = config.get("agent", {})
    if not isinstance(agent_cfg, dict):
        agent_cfg = {}
    raw = os.environ.get(
        "AGENELF_NO_PROGRESS_REPEAT_LIMIT",
        str(agent_cfg.get("no_progress_repeat_limit", _DEFAULT_NO_PROGRESS_LIMIT)),
    )
    return _bounded_int(raw, _DEFAULT_NO_PROGRESS_LIMIT, 2, 10)


def _normalize_progress_text(value: object) -> str:
    text = redact_sensitive_text(str(value or ""))
    text = _DYNAMIC_ID_RE.sub("<dynamic-id>", text)
    text = _TIMESTAMP_RE.sub("<timestamp>", text)
    return " ".join(text.split())[:4000]


def _batch_signature(records: list[dict[str, Any]]) -> str:
    normalized = []
    for record in records:
        normalized.append(
            {
                "name": str(record.get("name", "")),
                "arguments": record.get("arguments", {})
                if isinstance(record.get("arguments"), dict)
                else {},
                "result": _normalize_progress_text(record.get("result", "")),
            }
        )
    encoded = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _checkpoint_interrupted_task(
    user_input: str,
    *,
    reason: str,
    headline: str,
    detail: str,
    completed_rounds: int,
    segments: int,
) -> str:
    safe_detail = redact_sensitive_text(detail)[:3000]
    try:
        continuation = importlib.import_module("skills.task_continuation")
        value = continuation.checkpoint(
            task_summary=user_input,
            resume_prompt=(
                "继续完成原始任务。先读取已有工具结果、测试证据、运维请求和晋升状态，"
                "不要重复上一轮无进展调用。若上次是模型传输失败，重新获取当前状态后再继续；"
                "若目标涉及宿主机控制面，则转为人类主导仓库变更。"
                "只有真实完成、等待具体审批或存在明确外部阻塞时才结束。"
            ),
            reason=reason,
            expires_minutes=1440,
            max_attempts=3,
        )
        continuation_id = str(value.get("id", ""))
        return (
            f"{headline}\n\n"
            f"已执行模型轮次：{completed_rounds}；有界工具段：{segments}\n"
            f"最后证据：{safe_detail or '（无）'}\n\n"
            f"已保存可恢复检查点：{continuation_id or '（ID 未返回）'}\n"
            "CLI 保持可用；下次进入时会从检查点继续，不会伪装成任务完成。"
        )
    except Exception as exc:
        safe_error = redact_sensitive_text(f"{type(exc).__name__}: {exc}")[:1000]
        return (
            f"{headline}\n\n"
            f"已执行模型轮次：{completed_rounds}；有界工具段：{segments}\n"
            f"最后证据：{safe_detail or '（无）'}\n"
            f"保存续跑检查点失败：{safe_error}\n"
            "CLI 保持可用，请修复该控制面问题后继续当前任务。"
        )


def _checkpoint_exhausted_task(
    user_input: str,
    *,
    completed_rounds: int,
    segments: int,
) -> str:
    return _checkpoint_interrupted_task(
        user_input,
        reason="automatic_tool_budget_exhaustion",
        headline=(
            f"当前任务已自动续跑 {segments} 个工具段、共 {completed_rounds} 个模型轮次，"
            "仍未真实完成。"
        ),
        detail="总工具预算耗尽",
        completed_rounds=completed_rounds,
        segments=segments,
    )


class Agent:
    """Conversation loop plus composable, owner-local continuity."""

    def __init__(self, config: dict):
        self.config = config
        if config.get("local_dir"):
            os.environ["AGENELF_LOCAL_DIR"] = str(config["local_dir"])
        if config.get("servers_path"):
            os.environ["AGENELF_SERVERS_FILE"] = str(config["servers_path"])
        if config.get("validation_path"):
            os.environ["AGENELF_VALIDATION_FILE"] = str(config["validation_path"])
        if config.get("repositories_path"):
            os.environ["AGENELF_REPOSITORIES_FILE"] = str(config["repositories_path"])
        llm_cfg = config.get("llm", {})
        api_key = llm_cfg.get("api_key") or os.environ.get("OPENAI_API_KEY", "")
        if config.get("mock") or not api_key:
            self.llm: LLMClient = MockLLM(config)
        else:
            self.llm = LLMClient(config)

        # Executable Python extensions are disabled by default.  Importing an
        # app-space module executes its top-level code inside the Agent process, so
        # the owner must explicitly opt in before that directory is even scanned.
        extra_skills_dirs: list[str] = []
        runtime_root = config.get("runtime_root") or os.environ.get("AGENELF_ROOT")
        app_space_enabled = os.environ.get("AGENELF_ENABLE_APP_SPACE_SKILLS", "0") == "1"
        if runtime_root and app_space_enabled:
            appspace_skills = Path(runtime_root) / "app-space" / "skills"
            try:
                appspace_skills.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            extra_skills_dirs.append(str(appspace_skills))
        self.registry = SkillRegistry(
            config.get("skills_dir", "skills"),
            extra_skills_dirs=extra_skills_dirs or None,
            policy_engine=PolicyEngine(
                config.get("policy_dir")
                or str(
                    (Path(__file__).resolve().parents[2] / "policy").resolve()
                )
            ),
        )
        self.registry.discover()

        agent_cfg = config.get("agent", {})
        memory_path = config.get("memory_path", os.path.join("memory_store", "memory.json"))
        self.memory = MemoryStore(
            memory_path,
            max_entries=int(agent_cfg.get("memory_max_entries", 1000)),
        )
        self._memory_prompt_limit_default = int(agent_cfg.get("memory_prompt_limit", 50))
        self._memory_prompt_max_chars_default = int(agent_cfg.get("memory_prompt_max_chars", 8000))
        # 自我优化快车道：只覆盖白名单内的运行期参数，绝不修改 config.yaml
        opt_local = config.get("local_dir") or os.environ.get("AGENELF_LOCAL_DIR") or str(Path(memory_path).resolve().parent)
        self.optimization = SelfOptimizationStore(
            config.get("self_dir") or str(Path(opt_local) / "self"),
            root=config.get("runtime_root") or os.environ.get("AGENELF_ROOT"),
        )
        self._apply_optimization_overrides()
        self.max_tool_rounds = int(agent_cfg.get("max_tool_rounds", 8))
        self.history_max_messages = max(0, int(agent_cfg.get("history_max_messages", 12)))
        # 多会话历史：session_id -> 消息桶；None 统一落入 DEFAULT_SESSION_ID 桶，
        # 与旧的单一 self.history 行为完全一致（向后兼容）。
        self._sessions: OrderedDict[str, list[dict]] = OrderedDict()
        self._sessions[DEFAULT_SESSION_ID] = []
        # 显式有序钩子管线：技能通过 add_llm_wrapper / add_cycle_guard 注册，
        # 同名覆盖（幂等），应用顺序只由 priority 决定，不依赖技能文件名排序。
        self._llm_wrappers: dict[str, tuple[int, Callable[..., Any]]] = {}
        self._cycle_guards: dict[str, tuple[int, Callable[..., Any]]] = {}
        # 分段工具预算（单一主回路）：每轮 chat 会按当前配置重算并回写。
        self.max_tool_segments = configured_segments(config)
        self.no_progress_repeat_limit = configured_no_progress_limit(config)
        self.max_total_tool_rounds = (
            max(1, self.max_tool_rounds) * self.max_tool_segments
        )
        self.last_auto_reflection: dict | None = None
        self.last_auto_reflection_error = ""

        local_cfg = config.get("local_context", {})
        if not isinstance(local_cfg, dict):
            local_cfg = {}
        local_dir = config.get("local_dir") or os.environ.get("AGENELF_LOCAL_DIR")
        if not local_dir:
            local_dir = str(Path(memory_path).resolve().parent)
        self.local_context = LocalContextStore(
            local_dir,
            profile_path=config.get("local_profile_path"),
            preferences_path=config.get("local_preferences_path"),
            context_dir=config.get("local_context_dir"),
            servers_path=config.get("servers_path"),
            prompt_max_chars=int(local_cfg.get("prompt_max_chars", 12_000)),
            file_max_chars=int(local_cfg.get("file_max_chars", 20_000)),
            max_context_files=int(local_cfg.get("max_context_files", 20)),
        )

        persona_path = config.get("persona_path", os.path.join("persona", "persona.yaml"))
        self.persona = {} if self.local_context.profile else load_persona(persona_path)
        self.development = SelfDevelopmentEngine(self)
        self.system_prompt = ""
        self.configure_skill_runtimes()
        self._refresh_system_prompt()

    def configure_skill_runtimes(self, name: str | None = None) -> None:
        """Inject the owning Agent into skills that explicitly request runtime context."""

        items = (
            [(name, self.registry.skills[name])]
            if name and name in self.registry.skills
            else list(self.registry.skills.items())
        )
        for skill_name, module in items:
            configure = getattr(module, "configure_runtime", None)
            if not callable(configure):
                continue
            try:
                configure(agent=self, registry=self.registry, config=self.config)
                self.registry.errors.pop(f"runtime:{skill_name}", None)
            except Exception as exc:
                self.registry.errors[f"runtime:{skill_name}"] = (
                    f"运行时绑定失败：{type(exc).__name__}: {exc}"
                )

    def _apply_optimization_overrides(self) -> None:
        """用自我优化白名单内的有效覆盖值刷新运行期参数（无覆盖则回默认值）。"""

        self.memory_prompt_limit = int(self.optimization.get_effective("agent.memory_prompt_limit", self._memory_prompt_limit_default))
        self.memory_prompt_max_chars = int(self.optimization.get_effective("agent.memory_prompt_max_chars", self._memory_prompt_max_chars_default))

    def _refresh_system_prompt(self) -> None:
        self.local_context.reload()
        if self.local_context.profile:
            self.persona = {}
        # 自我优化覆盖在每轮刷新时重新生效（记忆块每次重建，无缓存）
        self._apply_optimization_overrides()
        self.system_prompt = build_system_prompt(
            self.persona,
            self.memory.as_prompt_block(
                limit=self.memory_prompt_limit,
                max_chars=self.memory_prompt_max_chars,
            ),
            self.registry.all_tool_schemas(),
            agent_name=self.config.get("agent", {}).get("name", "Agenelf"),
            capability_catalog=self.registry.capability_catalog(),
            local_context_block=self.local_context.prompt_block(),
            self_development_block=self.development.prompt_block(),
        )

    # ------------------------------------------------------------------
    # 多会话历史（按 session_id 分桶；None 走默认桶，向后兼容）
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_session_id(session_id: str | None) -> str:
        if session_id is None:
            return DEFAULT_SESSION_ID
        key = str(session_id).strip()
        return key or DEFAULT_SESSION_ID

    def _session_bucket(self, session_id: str | None = None) -> list[dict]:
        key = self._normalize_session_id(session_id)
        bucket = self._sessions.get(key)
        if bucket is None:
            if len(self._sessions) >= _MAX_SESSION_BUCKETS:
                # 驱逐最久未使用的桶（默认桶也会被公平对待，需要时会重建）
                self._sessions.popitem(last=False)
            bucket = []
            self._sessions[key] = bucket
        else:
            self._sessions.move_to_end(key)
        return bucket

    @property
    def history(self) -> list[dict]:
        """默认会话桶（session_id=None）的消息列表，与旧版单一历史语义一致。"""

        return self._session_bucket(None)

    @history.setter
    def history(self, value: list[dict]) -> None:
        self._sessions[DEFAULT_SESSION_ID] = list(value)

    def session_ids(self) -> list[str]:
        """当前进程内存在的会话桶 ID（含默认桶，按最近使用排序）。"""

        return list(self._sessions.keys())

    def get_history(
        self, session_id: str | None = None, limit: int | None = None
    ) -> list[dict]:
        """读取指定会话桶历史的副本；``limit`` 只取最近 N 条。"""

        entries = list(self._session_bucket(session_id))
        if limit is not None:
            entries = entries[-max(0, int(limit)) :]
        return entries

    def clear_history(self, session_id: str | None = None) -> int:
        """清空指定会话桶（None 清默认桶），返回清除的消息条数。"""

        bucket = self._session_bucket(session_id)
        cleared = len(bucket)
        del bucket[:]
        return cleared

    def _append_history(
        self, user_input: str, final_text: str, session_id: str | None = None
    ) -> None:
        if self.history_max_messages <= 0:
            return
        bucket = self._session_bucket(session_id)
        bucket.extend(
            [
                {"role": "user", "content": user_input},
                {"role": "assistant", "content": final_text},
            ]
        )
        if len(bucket) > self.history_max_messages:
            del bucket[: len(bucket) - self.history_max_messages]
            if bucket and bucket[0].get("role") == "assistant":
                del bucket[0]

    # ------------------------------------------------------------------
    # 显式有序钩子管线（替代基于文件名排序的猴子补丁）
    # ------------------------------------------------------------------
    def add_llm_wrapper(
        self, fn: Callable[..., Any], *, priority: int, name: str
    ) -> None:
        """注册一个 LLM 调用包装器，按 priority 显式排序应用。

        包装器协议：``fn(call_next, messages, tools=None) -> dict``，其中
        ``call_next(messages, tools=None)`` 调用内层（更接近 ``llm.chat`` 本体）。
        **priority 数值越大越外层**：外层包装器最先看到请求、最后处理内层抛出
        的异常；传输重试这类需要包裹所有其他层的包装器应使用较大 priority
        （例如 ``zz_transport_resilience`` 用 1000，保持旧的“最后加载=最外层”
        语义，但不再依赖技能文件名排序）。

        同名注册会覆盖旧实现，因此同一技能重复 ``configure_runtime`` 不会叠加
        多层包装（幂等）。
        """

        if not callable(fn):
            raise TypeError("llm wrapper 必须可调用")
        self._llm_wrappers[str(name)] = (int(priority), fn)

    def add_cycle_guard(
        self, fn: Callable[..., Any], *, priority: int, name: str
    ) -> None:
        """注册一个 ``run_autonomy_cycle`` 守卫，按 priority 显式排序应用。

        守卫协议：``fn(call_next, goal="", apply_changes=False) -> dict``，
        ``call_next(goal=..., apply_changes=...)`` 调用内层（最终进入受控沙盒
        执行）。与 ``add_llm_wrapper`` 相同，**priority 越大越外层**，同名覆盖
        保证幂等。
        """

        if not callable(fn):
            raise TypeError("cycle guard 必须可调用")
        self._cycle_guards[str(name)] = (int(priority), fn)

    def list_hooks(self) -> dict[str, list[dict[str, Any]]]:
        """列出已注册钩子（按应用顺序：最外层在前），便于诊断与审计。"""

        def rows(registry: dict[str, tuple[int, Callable[..., Any]]]) -> list[dict]:
            return [
                {"name": name, "priority": priority}
                for name, (priority, _fn) in sorted(
                    registry.items(), key=lambda item: (-item[1][0], item[0])
                )
            ]

        return {
            "llm_wrappers": rows(self._llm_wrappers),
            "cycle_guards": rows(self._cycle_guards),
        }

    def _call_llm(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> dict:
        """按 priority 顺序组合已注册包装器后调用 ``self.llm.chat``。

        组合发生在调用时刻、并延迟绑定 ``self.llm``，因此测试或运行期替换
        ``agent.llm`` 后包装器依然生效，且不会修改 LLM 客户端本体。
        """

        def base(msgs: list[dict], tools: list[dict] | None = None) -> dict:
            return self.llm.chat(msgs, tools=tools)

        call = base
        # 升序包裹：priority 最大的最后被包上，成为最外层。
        for _name, (_priority, fn) in sorted(
            self._llm_wrappers.items(), key=lambda item: (item[1][0], item[0])
        ):
            nxt = call

            def call(msgs, tools=None, fn=fn, nxt=nxt):  # noqa: B023
                return fn(nxt, msgs, tools)

        return call(messages, tools)

    def _close_reasoning_display(self) -> None:
        close = getattr(getattr(self, "llm", None), "close_reasoning_display", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def _maybe_auto_reflect(self) -> None:
        """Best-effort deterministic sedimentation; never break a completed chat."""

        try:
            result = self.development.maybe_reflect(trigger="conversation")
            if result is not None:
                self.last_auto_reflection = result
            self.last_auto_reflection_error = ""
            self.registry.errors.pop("runtime:self_development:auto", None)
        except Exception as exc:
            self.last_auto_reflection_error = f"{type(exc).__name__}: {exc}"
            self.registry.errors["runtime:self_development:auto"] = (
                f"自动沉淀失败：{self.last_auto_reflection_error}"
            )

    def _refresh_turn_runtime(
        self, messages: list[dict], continuation_note: str = ""
    ) -> list[dict] | None:
        self._refresh_system_prompt()
        prompt = str(self.system_prompt)
        if continuation_note:
            prompt += "\n\n" + continuation_note
        if messages and messages[0].get("role") == "system":
            messages[0] = {"role": "system", "content": prompt}
        else:
            messages.insert(0, {"role": "system", "content": prompt})
        return self.registry.all_tool_schemas() or None

    def _dispatch_tool(self, call: dict[str, Any], subject: str) -> str:
        name = str(call.get("name", ""))
        arguments = call.get("arguments", {})
        if not isinstance(arguments, dict):
            arguments = {}
        try:
            return str(self.registry.dispatch(name, arguments, subject=subject))
        except TypeError as exc:
            # Compatibility for tests or extensions that monkeypatch the old
            # two-argument dispatch signature. Real SkillRegistry supports subject.
            if "unexpected keyword argument 'subject'" not in str(exc):
                raise
            return str(self.registry.dispatch(name, arguments))

    @staticmethod
    def _assistant_tool_message(
        response: dict[str, Any], tool_calls: list[dict[str, Any]]
    ) -> dict[str, Any]:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": response.get("content"),
            "tool_calls": [
                {
                    "id": str(call.get("id", "")),
                    "type": "function",
                    "function": {
                        "name": str(call.get("name", "")),
                        "arguments": json.dumps(
                            call.get("arguments", {})
                            if isinstance(call.get("arguments", {}), dict)
                            else {},
                            ensure_ascii=False,
                        ),
                    },
                }
                for call in tool_calls
            ],
        }
        if response.get("reasoning_content"):
            message["reasoning_content"] = response.get("reasoning_content")
        return message

    def chat(
        self,
        user_input: str,
        *,
        subject: str = "agent",
        session_id: str | None = None,
    ) -> str:
        """单一主对话回路：分段工具预算 + 检查点续跑 + 有序钩子 + 多会话历史。

        这是唯一的对话主循环（原 ``Agent.chat`` 与 ``continuous_chat`` 两套
        近同实现已合并）。一次任务可跨多个有界工具段执行，同时保留同一模型
        上下文；检测到重复无进展工具批次或总预算耗尽时，保存重启安全的
        续跑检查点而不是返回固定失败文本。模型请求统一走 ``_call_llm``
        （priority 有序的包装器管线）。``session_id`` 选择历史桶，None 走
        默认桶（与旧行为一致）。
        """

        # 自我优化快车道：每轮对话前应用白名单内的温度覆盖；MockLLM 没有
        # 该属性也可直接赋值，其他异常一律容错，绝不影响对话主流程
        try:
            self.llm.temperature = float(
                self.optimization.get_effective(
                    "llm.temperature",
                    self.config.get("llm", {}).get("temperature", 0.6),
                )
            )
        except (AttributeError, TypeError, ValueError):
            pass

        self._refresh_system_prompt()
        messages: list[dict] = [
            {"role": "system", "content": self.system_prompt},
            *self._session_bucket(session_id),
            {"role": "user", "content": user_input},
        ]
        tools = self.registry.all_tool_schemas() or None
        final_text = ""
        tool_used = False
        completed_rounds = 0
        continuation_note = ""
        last_batch_signature = ""
        repeated_batches = 0
        last_batch_summary = ""

        segment_rounds = _bounded_int(self.max_tool_rounds, 8, 1, _MAX_ROUNDS_PER_SEGMENT)
        segments = configured_segments(self.config)
        repeat_limit = configured_no_progress_limit(self.config)
        total_round_budget = segment_rounds * segments
        self.max_tool_segments = segments
        self.max_total_tool_rounds = total_round_budget
        self.no_progress_repeat_limit = repeat_limit

        for round_index in range(total_round_budget):
            completed_rounds = round_index + 1
            try:
                response = self._call_llm(messages, tools)
            except Exception as exc:
                self._close_reasoning_display()
                final_text = _checkpoint_interrupted_task(
                    user_input,
                    reason="llm_request_failure",
                    headline="模型请求在有界重试后仍失败，当前 CLI 没有退出。",
                    detail=f"{type(exc).__name__}: {exc}",
                    completed_rounds=completed_rounds,
                    segments=segments,
                )
                break
            if not isinstance(response, dict):
                final_text = _checkpoint_interrupted_task(
                    user_input,
                    reason="invalid_model_response",
                    headline="模型返回了无效响应，当前任务已安全暂停。",
                    detail=f"response_type={type(response).__name__}",
                    completed_rounds=completed_rounds,
                    segments=segments,
                )
                break

            raw_calls = response.get("tool_calls") or []
            tool_calls = [call for call in raw_calls if isinstance(call, dict)]
            if not tool_calls:
                final_text = str(response.get("content") or "")
                break

            messages.append(self._assistant_tool_message(response, tool_calls))
            batch_records: list[dict[str, Any]] = []
            for call in tool_calls:
                tool_used = True
                result = self._dispatch_tool(call, subject)
                batch_records.append(
                    {
                        "name": str(call.get("name", "")),
                        "arguments": call.get("arguments", {})
                        if isinstance(call.get("arguments", {}), dict)
                        else {},
                        "result": result,
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(call.get("id", "")),
                        "name": str(call.get("name", "")),
                        "content": result,
                    }
                )

            signature = _batch_signature(batch_records)
            if signature == last_batch_signature:
                repeated_batches += 1
            else:
                last_batch_signature = signature
                repeated_batches = 1
            last_batch_summary = "; ".join(
                f"{record['name']}: {_normalize_progress_text(record['result'])[:500]}"
                for record in batch_records
            )
            if repeated_batches >= repeat_limit:
                final_text = _checkpoint_interrupted_task(
                    user_input,
                    reason="automatic_no_progress_loop",
                    headline=(
                        f"检测到同一工具结果连续重复 {repeated_batches} 次，已停止无进展循环。"
                    ),
                    detail=last_batch_summary,
                    completed_rounds=completed_rounds,
                    segments=segments,
                )
                break

            if (
                completed_rounds % segment_rounds == 0
                and completed_rounds < total_round_budget
            ):
                current_segment = completed_rounds // segment_rounds
                continuation_note = (
                    "【运行时自动续跑】\n"
                    f"已完成第 {current_segment}/{segments} 个有界工具段，当前仍是同一用户任务。"
                    "继续使用已有工具结果和当前最新技能完成最初目标；不要重复无进展调用。"
                    "若连续获得相同失败，停止并报告确定性根因，不得修改测试或策略绕过。"
                )
            tools = self._refresh_turn_runtime(messages, continuation_note)
        else:
            final_text = _checkpoint_exhausted_task(
                user_input,
                completed_rounds=completed_rounds,
                segments=segments,
            )

        if not final_text:
            final_text = "（未获得有效回复）"

        self._append_history(user_input, final_text, session_id=session_id)
        summary = f"用户：{user_input} | 助手：{final_text[:200]}"
        if tool_used:
            summary = (
                f"[含工具调用 rounds={completed_rounds}/{total_round_budget}] "
                + summary
            )
        self.memory.add("episode", summary)
        self._maybe_auto_reflect()
        self._refresh_system_prompt()
        return final_text

    def local_status(self) -> dict:
        return {
            **self.local_context.status(),
            "memory": self.memory.stats(),
        }

    def reload_local_context(self) -> dict:
        status = self.local_context.reload()
        self.persona = {} if self.local_context.profile else load_persona(
            self.config.get("persona_path", os.path.join("persona", "persona.yaml"))
        )
        self._refresh_system_prompt()
        return {
            **status,
            "memory": self.memory.stats(),
            "self_development": self.development.summary_for_snapshot(),
        }

    def remember_owner(self, kind: str, content: str) -> dict:
        stored = self.memory.add(kind, content)
        self._refresh_system_prompt()
        return {"stored": stored, "kind": kind, "memory": self.memory.stats()}

    def recall_owner(self, query: str, limit: int = 5) -> list[str]:
        return self.memory.recall(query, limit=limit)

    def self_snapshot(self) -> dict:
        snapshot = AutonomyEngine(self).snapshot()
        snapshot["self_development"] = self.development.summary_for_snapshot()
        return snapshot

    def self_assess(self) -> dict:
        return AutonomyEngine(self).assess()

    def capability_health(self) -> dict:
        root = (
            self.config.get("runtime_root")
            or os.environ.get("AGENELF_ROOT")
            or Path(__file__).resolve().parents[2]
        )
        return CapabilityHealth(root).snapshot()

    def improvement_roadmap(self, limit: int = 10) -> dict:
        root = (
            self.config.get("runtime_root")
            or os.environ.get("AGENELF_ROOT")
            or Path(__file__).resolve().parents[2]
        )
        intentions = self.improvement_intentions(limit=100)
        return CapabilityHealth(root).roadmap(intentions, limit=limit)

    def self_development_status(self) -> dict:
        return self.development.status()

    def self_reflections(self, limit: int = 10) -> list[dict]:
        return self.development.recent_reflections(limit)

    def reflect_and_sediment(self, note: str = "", deep: bool = False) -> dict:
        result = self.development.reflect(
            trigger="manual",
            note=note,
            deep=deep,
        )
        self._refresh_system_prompt()
        return result

    def improvement_intentions(
        self, status: str = "", limit: int = 20
    ) -> list[dict]:
        return self.development.list_intentions(status=status, limit=limit)

    def get_improvement_intention(self, intention_id: str) -> dict:
        return self.development.get_intention(intention_id)

    def create_improvement_intention(
        self,
        *,
        title: str,
        rationale: str = "",
        priority: str = "P2",
        acceptance_criteria: list[str] | None = None,
    ) -> dict:
        result = self.development.create_intention(
            title=title,
            rationale=rationale,
            priority=priority,
            acceptance_criteria=acceptance_criteria,
            source="owner_or_agent",
            owner_aligned=True,
        )
        self._refresh_system_prompt()
        return result

    def pursue_improvement_intention(
        self, intention_id: str, *, apply_changes: bool = False
    ) -> dict:
        result = self.development.pursue_intention(
            intention_id,
            apply_changes=apply_changes,
        )
        self._refresh_system_prompt()
        return result

    def run_autonomy_cycle(
        self, goal: str = "", apply_changes: bool = False
    ) -> dict:
        """受控自主循环，外层按 priority 顺序应用已注册的 cycle 守卫。"""

        def core(goal: str = "", apply_changes: bool = False) -> dict:
            result = AutonomyEngine(self).run_cycle(
                goal=goal,
                apply_changes=apply_changes,
            )
            self.development.observe_cycle(result)
            self._refresh_system_prompt()
            return result

        call = core
        # 升序包裹：priority 最大的最后被包上，成为最外层守卫。
        for _name, (_priority, guard) in sorted(
            self._cycle_guards.items(), key=lambda item: (item[1][0], item[0])
        ):
            nxt = call

            def call(
                goal: str = "",
                apply_changes: bool = False,
                *,
                _guard=guard,
                _nxt=nxt,
            ):
                return _guard(_nxt, goal, apply_changes)

        return call(goal, apply_changes)

    def autonomy_status(self, cycle_id: str = "") -> dict | list[dict]:
        engine = AutonomyEngine(self)
        return engine.get_cycle(cycle_id) if cycle_id else engine.latest_cycles()

    def evolve_skill(self, description: str) -> str:
        """Legacy entrypoint retained as a safe diagnostic response.

        Direct skill registration used to write model-generated Python into the
        running Agent process.  That path is now disabled.  Generic repository
        changes use code.repair; Agenelf's own core changes use the controlled
        app-tmp -> tests -> gate -> host-promotion pipeline.
        """

        del description
        return (
            "直接技能热加载已禁用（默认）。外部代码请使用 code.repair 隔离修复；"
            "Agenelf 自身改动请使用 /pursue <intent-id> --apply 或 /autonomy。"
        )
