"""成长脉动技能：离线自主迭代演示产物。

返回一句带 UTC 时间戳的中文"成长脉动"文本；
当前技能数等运行事实可通过参数传入。纯标准库实现，不依赖其他技能。
"""

from __future__ import annotations

from datetime import datetime, timezone

SKILL_META = {
    "name": "growth_pulse",
    "description": "生成一句带时间戳的中文成长脉动文本，用于标记一次可验证的自我迭代。",
    "version": "0.1.0",
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "growth_pulse",
            "description": "返回一句带 UTC 时间戳的成长脉动文本，可附带主题与当前技能数。",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "可选主题词，默认空字符串。",
                    },
                    "skill_count": {
                        "type": "integer",
                        "description": "可选当前已加载技能数，不大于 0 时省略。",
                    },
                },
                "required": [],
            },
        },
    }
]


def _growth_pulse(args: dict) -> str:
    args = args if isinstance(args, dict) else {}
    topic = str(args.get("topic", "") or "").strip()
    try:
        skill_count = int(args.get("skill_count", 0) or 0)
    except (TypeError, ValueError):
        skill_count = 0
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    text = f"[{stamp}] 成长脉动"
    if topic:
        text += f"·{topic}"
    text += "：Agenelf 又完成一次小而可验证的前进"
    if skill_count > 0:
        text += f"，当前已加载 {skill_count} 个技能"
    return text + "。"


def execute(tool_name: str, args: dict) -> str:
    if tool_name == "growth_pulse":
        return _growth_pulse(args)
    known = ", ".join(sorted(t["function"]["name"] for t in TOOLS))
    return f"未知工具：{tool_name}，可用工具：{known}"
