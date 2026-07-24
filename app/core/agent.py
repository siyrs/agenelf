"""Agenelf core: conversation, tool orchestration, memory and controlled evolution."""

from __future__ import annotations

import ast
import json
import os

from .autonomy import AutonomyEngine
from .context import build_system_prompt, load_persona
from .llm import LLMClient, MockLLM
from .memory import MemoryStore
from .registry import SkillRegistry

_SKILL_PROTOCOL_DOC = """\
技能协议（必须严格遵守）：
1. 模块级定义 SKILL_META = {"name": "...", "description": "...", "version": "0.1.0"}
2. 可选定义 CAPABILITY_META，声明能力域、操作、风险级别与可组合能力。
3. 模块级定义 TOOLS: list[dict]，为 OpenAI function-calling schema 列表。
4. 模块级定义函数 def execute(tool_name: str, args: dict) -> str，任何情况下返回字符串。
5. 生成的技能不得绕过 core 权限、操作队列、只读挂载或宿主机审批。
6. 自我迭代只能修改 app-tmp，必须包含测试并通过 gate_check；不得直接操作 Git 主分支。
只输出 Python 源码本身，不要输出任何解释文字。"""


class Agent:
    """Conversation loop plus composable capability-backed tools."""

    def __init__(self, config: dict):
        self.config = config
        llm_cfg = config.get("llm", {})
        api_key = llm_cfg.get("api_key") or os.environ.get("OPENAI_API_KEY", "")
        if config.get("mock") or not api_key:
            self.llm: LLMClient = MockLLM(config)
        else:
            self.llm = LLMClient(config)

        self.registry = SkillRegistry(config.get("skills_dir", "skills"))
        self.registry.discover()

        memory_path = config.get("memory_path", os.path.join("memory_store", "memory.json"))
        self.memory = MemoryStore(memory_path)
        agent_cfg = config.get("agent", {})
        self.memory_prompt_limit = int(agent_cfg.get("memory_prompt_limit", 50))
        self.memory_prompt_max_chars = int(agent_cfg.get("memory_prompt_max_chars", 8000))
        self.max_tool_rounds = int(agent_cfg.get("max_tool_rounds", 8))
        self.history_max_messages = max(0, int(agent_cfg.get("history_max_messages", 12)))
        self.history: list[dict] = []

        persona_path = config.get("persona_path", os.path.join("persona", "persona.yaml"))
        self.persona = load_persona(persona_path)
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

    def _refresh_system_prompt(self) -> None:
        self.system_prompt = build_system_prompt(
            self.persona,
            self.memory.as_prompt_block(
                limit=self.memory_prompt_limit,
                max_chars=self.memory_prompt_max_chars,
            ),
            self.registry.all_tool_schemas(),
            agent_name=self.config.get("agent", {}).get("name", "Agenelf"),
            capability_catalog=self.registry.capability_catalog(),
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

    def chat(self, user_input: str) -> str:
        """Process one turn while preserving a bounded recent conversation."""

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
                                "arguments": json.dumps(call["arguments"], ensure_ascii=False),
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
        self._refresh_system_prompt()
        return final_text

    def self_snapshot(self) -> dict:
        return AutonomyEngine(self).snapshot()

    def self_assess(self) -> dict:
        return AutonomyEngine(self).assess()

    def run_autonomy_cycle(self, goal: str = "", apply_changes: bool = False) -> dict:
        return AutonomyEngine(self).run_cycle(goal=goal, apply_changes=apply_changes)

    def autonomy_status(self, cycle_id: str = "") -> dict | list[dict]:
        engine = AutonomyEngine(self)
        return engine.get_cycle(cycle_id) if cycle_id else engine.latest_cycles()

    def evolve_skill(self, description: str) -> str:
        """Generate a skill in writable development mode; production uses autonomy."""

        response = self.llm.chat(
            [
                {
                    "role": "system",
                    "content": "你是技能代码生成器，必须遵守能力边界与技能协议。",
                },
                {
                    "role": "user",
                    "content": f"请为以下需求生成一个新的技能文件源码：\n{description}\n\n{_SKILL_PROTOCOL_DOC}",
                },
            ]
        )
        source = (response.get("content") or "").strip()
        if not source:
            return "技能进化失败：LLM 未返回任何源码"
        if source.startswith("```"):
            lines = source.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            source = "\n".join(lines).strip()

        filename = "evolved_skill.py"
        try:
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == "SKILL_META"
                    for target in node.targets
                ):
                    metadata = ast.literal_eval(node.value)
                    if isinstance(metadata, dict) and metadata.get("name"):
                        filename = f"{metadata['name']}.py"
                    break
        except (SyntaxError, ValueError):
            pass

        ok, message = self.registry.register_new_skill(filename, source)
        if ok:
            skill_name = os.path.splitext(os.path.basename(filename))[0]
            self.configure_skill_runtimes(skill_name)
            self._refresh_system_prompt()
            return f"技能进化成功：{message}"
        return f"技能进化失败：{message}"
