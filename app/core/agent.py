"""Agenelf core: conversation, personalization and persistent self-development."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

from .autonomy import AutonomyEngine
from .capability_health import CapabilityHealth
from .context import build_system_prompt, load_persona
from .llm import LLMClient, MockLLM
from .local_context import LocalContextStore
from .memory import MemoryStore
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
        self.history: list[dict] = []
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

    def _append_history(self, user_input: str, final_text: str) -> None:
        if self.history_max_messages <= 0:
            return
        self.history.extend(
            [
                {"role": "user", "content": user_input},
                {"role": "assistant", "content": final_text},
            ]
        )
        if len(self.history) > self.history_max_messages:
            self.history = self.history[-self.history_max_messages :]
            if self.history and self.history[0].get("role") == "assistant":
                self.history = self.history[1:]

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

    def chat(self, user_input: str) -> str:
        """Process one turn while preserving bounded conversation and continuity."""

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
            *self.history,
            {"role": "user", "content": user_input},
        ]
        tools = self.registry.all_tool_schemas() or None
        final_text = ""
        tool_used = False

        for _ in range(self.max_tool_rounds):
            response = self.llm.chat(messages, tools=tools)
            tool_calls = response.get("tool_calls") or []
            if not tool_calls:
                final_text = response.get("content") or ""
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": response.get("content"),
                    "tool_calls": [
                        {
                            "id": call["id"],
                            "type": "function",
                            "function": {
                                "name": call["name"],
                                "arguments": json.dumps(
                                    call["arguments"], ensure_ascii=False
                                ),
                            },
                        }
                        for call in tool_calls
                    ],
                }
            )
            for call in tool_calls:
                tool_used = True
                result = self.registry.dispatch(call["name"], call["arguments"])
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "name": call["name"],
                        "content": result,
                    }
                )
        else:
            final_text = "（已达到最大工具调用轮数，任务尚未完成）"

        if not final_text:
            final_text = "（未获得有效回复）"

        self._append_history(user_input, final_text)
        summary = f"用户：{user_input} | 助手：{final_text[:200]}"
        if tool_used:
            summary = f"[含工具调用] {summary}"
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
        result = AutonomyEngine(self).run_cycle(
            goal=goal,
            apply_changes=apply_changes,
        )
        self.development.observe_cycle(result)
        self._refresh_system_prompt()
        return result

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
