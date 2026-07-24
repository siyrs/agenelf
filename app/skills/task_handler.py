"""task_handler 技能：处理简单需求与指派任务的落盘工具。

数据持久化到 ``workspace/tasks/``（给 agent 指派的任务保存目录；不存在则自动创建，
找不到项目根时回退到 app/memory_store/）：
- 待办事项：JSON 文件（todos.json，增量追加）；
- 笔记：文本文件（notes/<标题>.txt），可按标题读回。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

SKILL_META = {
    "name": "task_handler",
    "description": "处理简单需求：创建待办清单、保存/读取笔记，数据落盘到 workspace/tasks/ 目录（指派任务保存目录）。",
    "version": "0.1.0",
}

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "create_todo",
            "description": "把一组待办事项追加保存到 workspace/tasks/todos.json，返回确认信息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "待办事项文本列表。",
                    },
                },
                "required": ["items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_note",
            "description": "把一篇笔记以文本形式保存到 workspace/tasks/notes/ 目录，返回确认信息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "笔记标题（将作为文件名，自动做安全清洗）。",
                    },
                    "content": {
                        "type": "string",
                        "description": "笔记正文内容。",
                    },
                },
                "required": ["title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_note",
            "description": "按标题读取 workspace/tasks/notes/ 中已保存的笔记内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "要读取的笔记标题。",
                    },
                },
                "required": ["title"],
            },
        },
    },
]

# 存储目录覆盖（主要供测试隔离使用）；None 表示用默认的项目根下 memory_store/
_store_dir: Path | None = None


def set_store_dir(path: str | Path | None) -> None:
    """覆盖存储目录（主要供测试隔离使用）；传 None 恢复默认。"""
    global _store_dir
    _store_dir = None if path is None else Path(path)


def _get_store_dir() -> Path:
    """获取存储目录：优先项目根下 workspace/tasks/（AGENELF_ROOT 优先，
    否则按 app/ 上一级推断），推断不到项目根时回退 app/memory_store/。"""
    if _store_dir is not None:
        store = _store_dir
    else:
        import os
        env_root = os.environ.get("AGENELF_ROOT", "").strip()
        root = Path(env_root) if env_root else Path(__file__).resolve().parents[2]
        if (root / "workspace").is_dir() or env_root:
            store = root / "workspace" / "tasks"
        else:
            store = Path(__file__).resolve().parent.parent / "memory_store"
    store.mkdir(parents=True, exist_ok=True)
    return store


def _safe_title(title: str) -> str:
    """把笔记标题清洗为安全的文件名（防目录穿越）。"""
    # 仅保留中英文、数字、-、_、空格，其余替换为下划线
    cleaned = re.sub(r"[^\w一-鿿 \-]", "_", title, flags=re.UNICODE).strip()
    return cleaned[:80] or "untitled"


def create_todo(items: list) -> str:
    """把待办事项追加写入 todos.json，返回确认信息。"""
    if not isinstance(items, list) or not items:
        return "创建失败：items 必须是非空列表"
    clean_items = [str(i).strip() for i in items if str(i).strip()]
    if not clean_items:
        return "创建失败：items 中没有有效内容"
    todo_file = _get_store_dir() / "todos.json"
    data = {"todos": []}
    if todo_file.exists():
        try:
            data = json.loads(todo_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {"todos": []}  # 文件损坏则重建
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for item in clean_items:
        data["todos"].append({"item": item, "done": False, "created_at": now})
    todo_file.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return f"已创建 {len(clean_items)} 条待办，共 {len(data['todos'])} 条，保存于 {todo_file}"


def save_note(title: str, content: str) -> str:
    """保存笔记到 notes/<标题>.txt，返回确认信息。"""
    if not isinstance(title, str) or not title.strip():
        return "保存失败：title 不能为空"
    notes_dir = _get_store_dir() / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    note_file = notes_dir / f"{_safe_title(title)}.txt"
    try:
        note_file.write_text(content, encoding="utf-8")
    except OSError as exc:
        return f"保存失败：{exc}"
    return f"笔记《{title}》已保存到 {note_file}（{len(content)} 字符）"


def read_note(title: str) -> str:
    """按标题读取笔记内容；不存在时返回提示。"""
    if not isinstance(title, str) or not title.strip():
        return "读取失败：title 不能为空"
    note_file = _get_store_dir() / "notes" / f"{_safe_title(title)}.txt"
    if not note_file.exists():
        return f"笔记《{title}》不存在"
    try:
        return note_file.read_text(encoding="utf-8")
    except OSError as exc:
        return f"读取失败：{exc}"


_DISPATCH = {
    "create_todo": lambda a: create_todo(a.get("items", [])),
    "save_note": lambda a: save_note(a.get("title", ""), a.get("content", "")),
    "read_note": lambda a: read_note(a.get("title", "")),
}


def execute(tool_name: str, args: dict) -> str:
    """按协议路由工具调用；内部捕获所有异常并返回字符串。"""
    handler = _DISPATCH.get(tool_name)
    if handler is None:
        return f"未知工具：{tool_name}，可用工具：{', '.join(sorted(_DISPATCH))}"
    try:
        return handler(args or {})
    except Exception as exc:  # 兜底：协议要求永不抛异常
        return f"执行失败：{type(exc).__name__}: {exc}"
