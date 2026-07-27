"""task_board 技能：结构化任务板（workspace/tasks/board.json）。

把 workspace/tasks 从"待办/笔记落盘"升级为结构化任务板：
主人指派任务 → agent 分解步骤 → 逐步推进 → 证据关联 → 完成归档。

- 存储：``workspace/tasks/board.json``（root 探测：AGENELF_ROOT 优先，
  否则按 app/ 上一级推断；原子写入，损坏时重建空板不崩）。
- 有界：主板最多 max_tasks=200 条，完成的旧任务归档到
  ``board-archive.json``，保持主板精简。
- 审计：所有变更 best-effort 追加 ``logs/audit.log``
  （``[task_board] action=... id=...``），失败绝不影响主流程。
- 与改进意向协作：task_link_intention 只存意向 ID，不 import
  self_development；由 agent 自行用 pursue_improvement_intention 推进，
  推进产出的证据（晋升历史 ID、授权 ID、文件路径等）再回填 task_complete。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.storage import atomic_write_json as _atomic_json
from core.storage import now_iso as _now_iso
from core.storage import safe_text as _safe_text

SKILL_META = {
    "name": "task_board",
    "description": "结构化任务板：主人指派任务、分解步骤、逐步推进、证据关联、完成归档，落盘 workspace/tasks/board.json。",
    "version": "0.1.0",
}

CAPABILITY_META = {
    "id": "agent.task_board",
    "name": "结构化任务板",
    "description": (
        "把主人指派的任务分解为可推进的步骤序列，跟踪状态、关联证据并归档完成项；"
        "任务记录是 agent 私有工作区状态，不直接改动受管系统。"
    ),
    "version": "0.1.0",
    "domain": "agent-governance",
    "operations": [
        {"name": "task_list", "description": "按状态过滤列出任务与步骤进度", "risk": "read"},
        {"name": "task_create", "description": "创建任务并建议步骤分解", "risk": "change"},
        {"name": "task_advance", "description": "把某步推进为 doing/done", "risk": "change"},
        {"name": "task_complete", "description": "带证据完成任务", "risk": "change"},
        {"name": "task_block", "description": "标注任务阻塞原因", "risk": "change"},
        {"name": "task_link_intention", "description": "关联 self_development 改进意向 ID", "risk": "change"},
    ],
    "composes_with": [
        "agent.self_development",
        "agent.self_reflection",
        "software.validation",
    ],
}

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "task_create",
            "description": (
                "创建一条结构化任务。steps 为空时返回提示建议先分解步骤；"
                "任务保存到 workspace/tasks/board.json。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "任务标题。"},
                    "steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "分解后的步骤文本列表，建议 2-8 步。",
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["P0", "P1", "P2", "P3"],
                        "description": "优先级，默认 P2。",
                    },
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_list",
            "description": "按状态过滤列出任务（open/doing/done/blocked），含步骤进度 x/y。",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "状态过滤，空字符串表示全部。",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_advance",
            "description": (
                "把指定步骤向前推进：pending→doing→done，可附 note；"
                "全部步骤 done 时任务自动置为 done 并记录 done_at。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "task- 开头的任务 ID。"},
                    "step_index": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "步骤下标（从 0 开始）。",
                    },
                    "note": {"type": "string", "description": "本步进展备注。"},
                },
                "required": ["task_id", "step_index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_complete",
            "description": (
                "带证据完成任务。证据为纯文本引用，可指向 promotion-history ID、"
                "auth ID、文件路径、测试报告等。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "task- 开头的任务 ID。"},
                    "evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "证据引用列表（纯文本记录）。",
                    },
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_block",
            "description": "把任务标注为 blocked 并记录原因；恢复推进可用 task_advance。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "task- 开头的任务 ID。"},
                    "reason": {"type": "string", "description": "阻塞原因。"},
                },
                "required": ["task_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_link_intention",
            "description": (
                "把任务关联到 self_development 改进意向（只存 intent- ID，不直接调用）；"
                "由 agent 自行用 pursue_improvement_intention 推进该意向。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "task- 开头的任务 ID。"},
                    "intention_id": {
                        "type": "string",
                        "description": "intent- 开头的改进意向 ID。",
                    },
                },
                "required": ["task_id", "intention_id"],
            },
        },
    },
]

# ---------------------------------------------------------------------------
# 常量与存储位置
# ---------------------------------------------------------------------------

_MAX_TASKS = 200          # 主板有界上限
_MAX_ARCHIVE = 1000       # 归档同样有界，避免无限增长
_MAX_STEPS = 50           # 单任务步骤上限
_VALID_PRIORITIES = {"P0", "P1", "P2", "P3"}
_VALID_STATUSES = {"open", "doing", "done", "blocked"}
_STEP_STATUSES = {"pending", "doing", "done"}
_TASK_ID_RE = re.compile(r"task-[A-Za-z0-9._-]+")

# 存储目录覆盖（主要供测试隔离使用）；None 表示按默认规则探测
_store_dir: Path | None = None


def set_store_dir(path: str | Path | None) -> None:
    """覆盖存储目录（主要供测试隔离使用）；传 None 恢复默认。"""
    global _store_dir
    _store_dir = None if path is None else Path(path)


def _detect_root() -> Path:
    """项目根探测：AGENELF_ROOT 优先，否则按 app/ 上一级推断。"""
    env_root = os.environ.get("AGENELF_ROOT", "").strip()
    if env_root:
        return Path(env_root)
    return Path(__file__).resolve().parents[2]


def _get_store_dir() -> Path:
    """任务板存储目录：root/workspace/tasks/，找不到项目根时回退 app/memory_store/。"""
    if _store_dir is not None:
        store = _store_dir
    else:
        root = _detect_root()
        if (root / "workspace").is_dir() or os.environ.get("AGENELF_ROOT", "").strip():
            store = root / "workspace" / "tasks"
        else:
            store = Path(__file__).resolve().parent.parent / "memory_store"
    store.mkdir(parents=True, exist_ok=True)
    return store


def _audit_root() -> Path:
    """审计日志所在运行根：与存储目录保持一致的推断。"""
    if _store_dir is not None:
        # 覆盖目录约定为 <root>/workspace/tasks，上两级即运行根
        return _store_dir.parent.parent
    return _detect_root()


def _audit(action: str, detail: str) -> None:
    """Best-effort 审计：追加 logs/audit.log，失败绝不影响主流程。"""
    path = _audit_root() / "logs" / "audit.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] [task_board] action={action} {detail}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 原子读写与容错（共享实现见 core.storage）
# ---------------------------------------------------------------------------

def _load_board() -> dict[str, Any]:
    """读取主板；文件损坏或结构非法时重建空板（不崩）。"""
    path = _get_store_dir() / "board.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"tasks": []}
    if not isinstance(value, dict) or not isinstance(value.get("tasks"), list):
        return {"tasks": []}
    value["tasks"] = [t for t in value["tasks"] if isinstance(t, dict)]
    return value


def _load_archive() -> dict[str, Any]:
    """读取归档板；损坏时同样重建。"""
    path = _get_store_dir() / "board-archive.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"tasks": []}
    if not isinstance(value, dict) or not isinstance(value.get("tasks"), list):
        return {"tasks": []}
    value["tasks"] = [t for t in value["tasks"] if isinstance(t, dict)]
    return value


def _enforce_bound(board: dict[str, Any]) -> list[dict[str, Any]]:
    """有界约束：超过 _MAX_TASKS 时把最旧的完成（done）任务移出主板归档。"""
    tasks = board["tasks"]
    if len(tasks) <= _MAX_TASKS:
        return []
    archived: list[dict[str, Any]] = []
    # 优先移出已完成的旧任务（按 done_at/updated_at 升序）
    done = sorted(
        (t for t in tasks if t.get("status") == "done"),
        key=lambda t: str(t.get("done_at") or t.get("updated_at") or ""),
    )
    overflow = len(tasks) - _MAX_TASKS
    move = done[:overflow]
    # 完成任务不够时，移出最旧的非完成任务兜底，保证硬上界
    if len(move) < overflow:
        rest = sorted(
            (t for t in tasks if t.get("status") != "done"),
            key=lambda t: str(t.get("updated_at") or ""),
        )
        move.extend(rest[: overflow - len(move)])
    move_ids = {id(t) for t in move}
    board["tasks"] = [t for t in tasks if id(t) not in move_ids]
    archived.extend(move)
    return archived


def _save_board(board: dict[str, Any]) -> list[dict[str, Any]]:
    """原子写主板并执行有界归档；返回本次归档的任务列表。"""
    archived = _enforce_bound(board)
    store = _get_store_dir()
    _atomic_json(store / "board.json", board)
    if archived:
        archive = _load_archive()
        archive["tasks"].extend(archived)
        archive["tasks"] = archive["tasks"][-_MAX_ARCHIVE:]
        _atomic_json(store / "board-archive.json", archive)
    return archived


# ---------------------------------------------------------------------------
# 数据规范化
# ---------------------------------------------------------------------------

def _new_steps(steps: list[Any]) -> list[dict[str, str]]:
    """把输入步骤文本规范化为步骤结构。"""
    result: list[dict[str, str]] = []
    for item in steps[:_MAX_STEPS]:
        text = _safe_text(item, 500)
        if text:
            result.append({"text": text, "status": "pending", "note": ""})
    return result


def _new_task_id(title: str) -> str:
    """生成 task-<时间戳>-<hash6> 形式的任务 ID。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    digest = hashlib.sha256(f"{title}-{uuid.uuid4().hex}".encode("utf-8")).hexdigest()
    return f"task-{stamp}-{digest[:6]}"


def _find_task(board: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    for task in board["tasks"]:
        if task.get("id") == task_id:
            return task
    return None


def _progress(task: dict[str, Any]) -> str:
    steps = task.get("steps") or []
    done = sum(1 for s in steps if isinstance(s, dict) and s.get("status") == "done")
    return f"{done}/{len(steps)}"


def _summary(task: dict[str, Any]) -> dict[str, Any]:
    """列表视图：不含完整步骤文本，含步骤进度 x/y。"""
    return {
        "id": task.get("id"),
        "title": task.get("title"),
        "status": task.get("status"),
        "priority": task.get("priority"),
        "progress": _progress(task),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
        "done_at": task.get("done_at"),
        "evidence": list(task.get("evidence") or []),
        "linked_intention": task.get("linked_intention"),
        "block_reason": task.get("block_reason") or "",
    }


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------

def task_create(title: str, steps: list[Any] | None = None, priority: str = "P2") -> str:
    """创建任务；steps 为空时返回提示建议分解。"""
    title = _safe_text(title, 300)
    if not title:
        return _dump({"ok": False, "error": "创建失败：title 不能为空"})
    priority = str(priority or "P2").upper()
    if priority not in _VALID_PRIORITIES:
        return _dump(
            {"ok": False, "error": f"未知优先级 {priority!r}；应为 P0/P1/P2/P3"}
        )
    board = _load_board()
    now = _now_iso()
    task = {
        "id": _new_task_id(title),
        "title": title,
        "steps": _new_steps(steps if isinstance(steps, list) else []),
        "status": "open",
        "priority": priority,
        "created_at": now,
        "updated_at": now,
        "done_at": None,
        "evidence": [],
        "linked_intention": None,
        "block_reason": "",
    }
    board["tasks"].append(task)
    archived = _save_board(board)
    _audit("create", f"id={task['id']} priority={priority} steps={len(task['steps'])}")
    result: dict[str, Any] = {"ok": True, "task": task}
    if not task["steps"]:
        result["hint"] = "steps 为空：建议先把任务分解为 2-8 个可验证步骤再推进"
    if archived:
        result["archived"] = [t.get("id") for t in archived]
    return _dump(result)


def task_list(status: str = "") -> str:
    """按状态过滤列出任务（含步骤进度 x/y）。"""
    status = str(status or "").strip()
    if status and status not in _VALID_STATUSES:
        return _dump(
            {"ok": False, "error": f"未知状态 {status!r}；应为 open/doing/done/blocked"}
        )
    board = _load_board()
    tasks = [t for t in board["tasks"] if not status or t.get("status") == status]
    _audit("list", f"status={status or 'all'} count={len(tasks)}")
    return _dump(
        {
            "ok": True,
            "status": status or "all",
            "count": len(tasks),
            "tasks": [_summary(t) for t in tasks],
        }
    )


def task_advance(task_id: str, step_index: int, note: str = "") -> str:
    """把某步向前推进：pending→doing→done；全 done 则任务自动 done。"""
    task_id = str(task_id or "").strip()
    if not _TASK_ID_RE.fullmatch(task_id):
        return _dump({"ok": False, "error": f"非法任务 ID：{task_id!r}"})
    board = _load_board()
    task = _find_task(board, task_id)
    if task is None:
        return _dump({"ok": False, "error": f"任务不存在：{task_id}"})
    if task.get("status") == "done":
        return _dump({"ok": False, "error": f"任务 {task_id} 已完成，不能再推进步骤"})
    steps = task.get("steps") or []
    try:
        index = int(step_index)
    except (TypeError, ValueError):
        return _dump({"ok": False, "error": "step_index 必须是整数"})
    if index < 0 or index >= len(steps):
        return _dump(
            {"ok": False, "error": f"step_index 越界：{index}（共 {len(steps)} 步）"}
        )
    step = steps[index]
    if step.get("status") == "done":
        return _dump({"ok": False, "error": f"步骤 {index} 已是 done"})
    # 状态机：pending→doing→done
    step["status"] = "doing" if step.get("status") == "pending" else "done"
    note = _safe_text(note, 1000)
    if note:
        step["note"] = note
    now = _now_iso()
    task["updated_at"] = now
    auto_done = False
    if all(s.get("status") == "done" for s in steps) and steps:
        task["status"] = "done"
        task["done_at"] = now
        task["block_reason"] = ""
        auto_done = True
    else:
        task["status"] = "doing"
        task["block_reason"] = ""
    archived = _save_board(board)
    _audit(
        "advance",
        f"id={task_id} step={index} step_status={step['status']} task_status={task['status']}",
    )
    result: dict[str, Any] = {
        "ok": True,
        "task_id": task_id,
        "step_index": index,
        "step": dict(step),
        "task_status": task["status"],
        "progress": _progress(task),
    }
    if auto_done:
        result["auto_done"] = True
        result["done_at"] = now
    if archived:
        result["archived"] = [t.get("id") for t in archived]
    return _dump(result)


def task_complete(task_id: str, evidence: list[Any] | None = None) -> str:
    """带证据完成任务：剩余步骤标记 done，记录 evidence 与 done_at。"""
    task_id = str(task_id or "").strip()
    if not _TASK_ID_RE.fullmatch(task_id):
        return _dump({"ok": False, "error": f"非法任务 ID：{task_id!r}"})
    board = _load_board()
    task = _find_task(board, task_id)
    if task is None:
        return _dump({"ok": False, "error": f"任务不存在：{task_id}"})
    if task.get("status") == "done":
        return _dump({"ok": False, "error": f"任务 {task_id} 已是 done"})
    items: list[str] = []
    for raw in (evidence if isinstance(evidence, list) else [])[:20]:
        text = _safe_text(raw, 1000)
        if text:
            items.append(text)
    now = _now_iso()
    for step in task.get("steps") or []:
        if step.get("status") != "done":
            step["status"] = "done"
    task["status"] = "done"
    task["done_at"] = now
    task["updated_at"] = now
    task["block_reason"] = ""
    task["evidence"] = list(task.get("evidence") or []) + items
    archived = _save_board(board)
    _audit("complete", f"id={task_id} evidence={len(items)}")
    result: dict[str, Any] = {
        "ok": True,
        "task_id": task_id,
        "status": "done",
        "done_at": now,
        "evidence": task["evidence"],
    }
    if archived:
        result["archived"] = [t.get("id") for t in archived]
    return _dump(result)


def task_block(task_id: str, reason: str) -> str:
    """把任务标注为 blocked 并记录原因。"""
    task_id = str(task_id or "").strip()
    if not _TASK_ID_RE.fullmatch(task_id):
        return _dump({"ok": False, "error": f"非法任务 ID：{task_id!r}"})
    reason = _safe_text(reason, 1000)
    if not reason:
        return _dump({"ok": False, "error": "阻塞原因 reason 不能为空"})
    board = _load_board()
    task = _find_task(board, task_id)
    if task is None:
        return _dump({"ok": False, "error": f"任务不存在：{task_id}"})
    if task.get("status") == "done":
        return _dump({"ok": False, "error": f"任务 {task_id} 已完成，不能标记阻塞"})
    task["status"] = "blocked"
    task["block_reason"] = reason
    task["updated_at"] = _now_iso()
    _save_board(board)
    _audit("block", f"id={task_id} reason={reason[:100]}")
    return _dump(
        {
            "ok": True,
            "task_id": task_id,
            "status": "blocked",
            "block_reason": reason,
            "hint": "恢复推进：对下一步调用 task_advance 即可回到 doing",
        }
    )


def task_link_intention(task_id: str, intention_id: str) -> str:
    """关联 self_development 改进意向（只存 ID，不 import）。

    推进意向由 agent 自行调用 pursue_improvement_intention 完成；
    推进产出的证据可回填 task_complete。
    """
    task_id = str(task_id or "").strip()
    if not _TASK_ID_RE.fullmatch(task_id):
        return _dump({"ok": False, "error": f"非法任务 ID：{task_id!r}"})
    intention_id = str(intention_id or "").strip()
    if not intention_id.startswith("intent-"):
        return _dump(
            {"ok": False, "error": f"非法改进意向 ID：{intention_id!r}（应 intent- 开头）"}
        )
    board = _load_board()
    task = _find_task(board, task_id)
    if task is None:
        return _dump({"ok": False, "error": f"任务不存在：{task_id}"})
    task["linked_intention"] = intention_id
    task["updated_at"] = _now_iso()
    _save_board(board)
    _audit("link_intention", f"id={task_id} intention={intention_id}")
    return _dump(
        {
            "ok": True,
            "task_id": task_id,
            "linked_intention": intention_id,
            "hint": "已关联意向 ID；请用 pursue_improvement_intention 推进，证据回填 task_complete",
        }
    )


_DISPATCH = {
    "task_create": lambda a: task_create(
        a.get("title", ""), a.get("steps", []), a.get("priority", "P2")
    ),
    "task_list": lambda a: task_list(a.get("status", "")),
    "task_advance": lambda a: task_advance(
        a.get("task_id", ""), a.get("step_index", 0), a.get("note", "")
    ),
    "task_complete": lambda a: task_complete(a.get("task_id", ""), a.get("evidence", [])),
    "task_block": lambda a: task_block(a.get("task_id", ""), a.get("reason", "")),
    "task_link_intention": lambda a: task_link_intention(
        a.get("task_id", ""), a.get("intention_id", "")
    ),
}


def execute(tool_name: str, args: dict) -> str:
    """按协议路由工具调用；内部捕获所有异常并返回字符串。"""
    handler = _DISPATCH.get(tool_name)
    if handler is None:
        return _dump(
            {"ok": False, "error": f"未知工具：{tool_name}，可用工具：{', '.join(sorted(_DISPATCH))}"}
        )
    try:
        return handler(args or {})
    except Exception as exc:  # 兜底：协议要求永不抛异常
        return _dump({"ok": False, "error": f"执行失败：{type(exc).__name__}: {exc}"})
