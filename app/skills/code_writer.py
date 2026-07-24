"""code_writer 技能：写代码文件并执行 Python 片段。

安全约束：
- 写文件被限制在项目目录内，拒绝绝对路径逃逸和 ``../`` 穿越；
- 执行代码通过子进程运行，带 30 秒超时，stdout/stderr 全量捕获返回。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SKILL_META = {
    "name": "code_writer",
    "description": "代码写入与执行：在项目目录内写代码文件，并用子进程运行 Python 片段（30 秒超时）。",
    "version": "0.1.0",
}

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "write_code_file",
            "description": "在项目目录内写入一个代码文件（自动创建父目录），拒绝逃逸项目目录的路径，返回写入后的文件路径。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "相对于项目根目录的文件路径（如 scripts/hello.py）；也接受项目目录内的绝对路径。",
                    },
                    "content": {
                        "type": "string",
                        "description": "要写入的文件内容（UTF-8）。",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": "用子进程执行一段 Python 代码（30 秒超时），返回退出码、stdout 与 stderr。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "要执行的 Python 源代码片段。",
                    },
                },
                "required": ["code"],
            },
        },
    },
]

# 项目根目录（skills/ 的上级）；测试可通过 set_project_root 注入临时目录
_project_root: Path | None = None


def set_project_root(path: str | Path | None) -> None:
    """覆盖项目根目录（主要供测试隔离使用）；传 None 恢复默认。"""
    global _project_root
    _project_root = None if path is None else Path(path).resolve()


def _get_project_root() -> Path:
    """获取项目根目录，默认为本文件所在 skills/ 目录的上级。"""
    if _project_root is not None:
        return _project_root
    return Path(__file__).resolve().parent.parent


def write_code_file(path: str, content: str) -> str:
    """把 content 写入项目目录内的 path，返回绝对路径。"""
    if not isinstance(path, str) or not path.strip():
        return "写入失败：path 不能为空"
    root = _get_project_root()
    raw = Path(path)
    # 相对路径拼到项目根下；绝对路径保持原样，随后统一做包含校验
    candidate = raw if raw.is_absolute() else root / raw
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        return f"写入失败：路径无法解析：{exc}"
    # Python 3.10+：is_relative_to 防止 ../ 或绝对路径逃逸出项目目录
    if not resolved.is_relative_to(root):
        return f"写入失败：路径 {path!r} 逃逸出项目目录 {root}"
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
    except OSError as exc:
        return f"写入失败：{exc}"
    return str(resolved)


def run_python(code: str) -> str:
    """子进程执行 Python 片段，返回退出码与输出。"""
    if not isinstance(code, str) or not code.strip():
        return "执行失败：code 不能为空"
    try:
        child_env = os.environ.copy()
        # Windows 子进程默认沿用本地代码页；强制 UTF-8，和父进程的解码约定一致。
        child_env.setdefault("PYTHONIOENCODING", "utf-8")
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            cwd=str(_get_project_root()),
            env=child_env,
        )
    except subprocess.TimeoutExpired:
        return "执行超时（30 秒限制），进程已被终止"
    except OSError as exc:
        return f"执行失败：无法启动 Python 子进程：{exc}"
    parts = [f"退出码：{proc.returncode}"]
    parts.append(f"stdout:\n{proc.stdout}" if proc.stdout else "stdout:（空）")
    parts.append(f"stderr:\n{proc.stderr}" if proc.stderr else "stderr:（空）")
    return "\n".join(parts)


_DISPATCH = {
    "write_code_file": lambda a: write_code_file(a.get("path", ""), a.get("content", "")),
    "run_python": lambda a: run_python(a.get("code", "")),
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
