"""Agent 核心模块。

Agent 组装 LLM 客户端、技能注册中心、记忆存储与系统提示，
对外提供对话（chat）与技能进化（evolve_skill）两个入口。
"""

from __future__ import annotations

import json
import os

from .context import build_system_prompt, load_persona
from .llm import LLMClient, MockLLM
from .memory import MemoryStore
from .registry import SkillRegistry

# 生成新技能时给 LLM 的协议说明（与 core/registry.py 的校验逻辑保持一致）
_SKILL_PROTOCOL_DOC = """\
技能协议（必须严格遵守）：
1. 模块级定义 SKILL_META = {"name": "...", "description": "...", "version": "0.1.0"}
2. 模块级定义 TOOLS: list[dict]，为 OpenAI function-calling schema 列表，例如：
   [{"type": "function", "function": {"name": "工具名", "description": "...",
     "parameters": {"type": "object", "properties": {...}, "required": [...]}}}]
3. 模块级定义函数 def execute(tool_name: str, args: dict) -> str，
   内部必须自行捕获所有异常，任何情况下都返回字符串。
只输出 Python 源码本身，不要输出任何解释文字。"""


class Agent:
    """Agenelf 智能体核心：对话循环 + 工具调用 + 记忆沉淀。"""

    def __init__(self, config: dict):
        self.config = config

        # --- LLM 客户端：mock 强制开关或 api_key 为空时使用 MockLLM ---
        llm_cfg = config.get("llm", {})
        api_key = llm_cfg.get("api_key") or os.environ.get("OPENAI_API_KEY", "")
        if config.get("mock") or not api_key:
            self.llm: LLMClient = MockLLM(config)
        else:
            self.llm = LLMClient(config)

        # --- 技能注册中心 ---
        skills_dir = config.get("skills_dir", "skills")
        self.registry = SkillRegistry(skills_dir)
        self.registry.discover()

        # --- 长期记忆 ---
        memory_path = config.get("memory_path", os.path.join("memory_store", "memory.json"))
        self.memory = MemoryStore(memory_path)
        agent_cfg = config.get("agent", {})
        self.memory_prompt_limit = int(agent_cfg.get("memory_prompt_limit", 50))
        self.memory_prompt_max_chars = int(agent_cfg.get("memory_prompt_max_chars", 8000))

        # --- 系统提示：persona + 记忆 + 技能清单 ---
        persona_path = config.get("persona_path", os.path.join("persona", "persona.yaml"))
        self.persona = load_persona(persona_path)
        self.system_prompt = ""
        self._refresh_system_prompt()

        # 最大工具调用轮数，默认 8
        self.max_tool_rounds = int(agent_cfg.get("max_tool_rounds", 8))

    def _refresh_system_prompt(self) -> None:
        """根据最新记忆和已加载技能重建下一轮对话的系统提示。"""
        self.system_prompt = build_system_prompt(
            self.persona,
            self.memory.as_prompt_block(
                limit=self.memory_prompt_limit,
                max_chars=self.memory_prompt_max_chars,
            ),
            self.registry.all_tool_schemas(),
            agent_name=self.config.get("agent", {}).get("name", "Agenelf"),
        )

    # ------------------------------------------------------------------
    # 对话主循环
    # ------------------------------------------------------------------
    def chat(self, user_input: str) -> str:
        """处理一轮用户输入，返回最终文本回复。

        至多进行 max_tool_rounds 轮工具调用循环：
        LLM 返回 tool_calls 则分发执行并把结果追加进消息，直到返回纯文本。
        """
        # 每轮均刷新，既纳入上一轮写入的 episode，也纳入外部工具追加的记忆。
        self._refresh_system_prompt()
        messages: list[dict] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_input},
        ]
        tools = self.registry.all_tool_schemas() or None
        final_text = ""
        tool_used = False

        for _ in range(self.max_tool_rounds):
            resp = self.llm.chat(messages, tools=tools)
            tool_calls = resp.get("tool_calls") or []

            if not tool_calls:
                # LLM 给出最终文本，结束循环
                final_text = resp.get("content") or ""
                break

            # 记录 assistant 的工具调用回合（OpenAI 消息格式）
            messages.append(
                {
                    "role": "assistant",
                    "content": resp.get("content"),
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": json.dumps(
                                    tc["arguments"], ensure_ascii=False
                                ),
                            },
                        }
                        for tc in tool_calls
                    ],
                }
            )

            # 逐个分发工具调用，把结果作为 tool 消息追加
            for tc in tool_calls:
                tool_used = True
                result = self.registry.dispatch(tc["name"], tc["arguments"])
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": tc["name"],
                        "content": result,
                    }
                )
        else:
            # 循环耗尽仍未得到文本回复
            final_text = "（已达到最大工具调用轮数，未能生成最终回复）"

        if not final_text:
            final_text = "（未获得有效回复）"

        # 重要交互写入长期记忆（episode 类型）
        summary = f"用户：{user_input} | 助手：{final_text[:200]}"
        if tool_used:
            summary = f"[含工具调用] {summary}"
        self.memory.add("episode", summary)
        self._refresh_system_prompt()

        return final_text

    # ------------------------------------------------------------------
    # 技能进化
    # ------------------------------------------------------------------
    def evolve_skill(self, description: str) -> str:
        """让 LLM 按技能协议生成新技能源码并注册，返回中文结果说明。"""
        gen_messages = [
            {
                "role": "system",
                "content": "你是一个技能代码生成器，严格按给定协议输出可运行的 Python 源码。",
            },
            {
                "role": "user",
                "content": (
                    f"请为以下需求生成一个新的技能文件源码：\n{description}\n\n"
                    f"{_SKILL_PROTOCOL_DOC}"
                ),
            },
        ]
        resp = self.llm.chat(gen_messages)
        source = (resp.get("content") or "").strip()
        if not source:
            return "技能进化失败：LLM 未返回任何源码"

        # 剥离可能存在的 Markdown 代码围栏
        if source.startswith("```"):
            lines = source.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            source = "\n".join(lines).strip()

        # 从源码中推断技能名作为文件名，推断失败则用默认名
        filename = "evolved_skill.py"
        try:
            import ast

            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "SKILL_META"
                    for t in node.targets
                ):
                    meta = ast.literal_eval(node.value)
                    if isinstance(meta, dict) and meta.get("name"):
                        filename = f"{meta['name']}.py"
                    break
        except (SyntaxError, ValueError):
            pass

        ok, msg = self.registry.register_new_skill(filename, source)
        if ok:
            # 新技能加入后刷新系统提示中的技能清单
            self._refresh_system_prompt()
            return f"技能进化成功：{msg}"
        return f"技能进化失败：{msg}"
