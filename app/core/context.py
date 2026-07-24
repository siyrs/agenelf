"""系统提示组装模块。

将用户数字画像（persona）、长期记忆与技能清单拼装为
发给 LLM 的系统提示词，这是 Agenelf "以用户为原型" 人格的注入点。
"""

from __future__ import annotations

import os

import yaml


def load_persona(persona_path: str) -> dict:
    """加载 persona.yaml 用户数字画像；文件缺失或损坏时返回空 dict。"""
    if not os.path.exists(persona_path):
        return {}
    try:
        with open(persona_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except (yaml.YAMLError, OSError):
        return {}


def _render_persona_block(persona: dict) -> str:
    """把 persona 字典渲染为可读的画像文本块。"""
    if not persona:
        return "（用户画像尚未填写，请先完善 persona/persona.yaml）"
    lines: list[str] = []
    for key, value in persona.items():
        if isinstance(value, list):
            lines.append(f"- {key}: " + "、".join(str(v) for v in value))
        elif isinstance(value, dict):
            lines.append(f"- {key}:")
            for k, v in value.items():
                lines.append(f"    - {k}: {v}")
        else:
            lines.append(f"- {key}: {value}")
    return "\n".join(lines)


def _render_skills_block(tool_schemas: list[dict]) -> str:
    """把工具 schema 列表渲染为简洁的技能清单文本。"""
    if not tool_schemas:
        return "（当前没有已加载的技能）"
    lines: list[str] = []
    for tool in tool_schemas:
        fn = tool.get("function", {}) if isinstance(tool, dict) else {}
        name = fn.get("name", "未知工具")
        desc = fn.get("description", "")
        lines.append(f"- {name}: {desc}")
    return "\n".join(lines)


def build_system_prompt(
    persona: dict,
    memory_block: str,
    tool_schemas: list[dict],
    agent_name: str = "Agenelf",
) -> str:
    """组装完整系统提示：角色设定 + 用户画像 + 长期记忆 + 技能清单。"""
    return f"""你是 {agent_name}，一个以用户为原型构建的镜像智能体。
你的目标是以用户的思维方式、沟通风格和价值观来理解并协助用户，
在任务需要时通过调用技能工具来完成实际操作。

【用户数字画像】
{_render_persona_block(persona)}

【长期记忆】
{memory_block}

【可用技能工具】
{_render_skills_block(tool_schemas)}

请始终使用中文与用户交流，回复简洁、直接、贴合用户风格。"""
