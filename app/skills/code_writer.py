"""Compatibility scratch writer with in-process code execution permanently disabled.

Historically this skill could write anywhere inside the application tree and launch
arbitrary Python in the Agent process.  That violated Agenelf's split-runtime policy.
It now writes bounded text files only inside ``workspace/scratch``.  Executable
validation belongs to the isolated ``code.repair`` runner.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

SKILL_META = {
    "name": "code_writer",
    "description": "兼容型安全草稿写入：仅写 workspace/scratch 文本文件；任意 Python 执行已永久禁用。",
    "version": "1.0.0",
}

CAPABILITY_META = {
    "id": "code.scratch",
    "name": "代码草稿区",
    "description": "在隔离 scratch 目录保存有界文本草稿；不执行代码、不修改应用或受管仓库。",
    "version": "1.0.0",
    "domain": "development",
    "operations": [
        {"name": "write_code_file", "description": "写入 scratch 文本草稿", "risk": "change"},
        {"name": "run_python", "description": "旧任意 Python 执行入口，永久禁止", "risk": "forbidden"},
    ],
    "composes_with": ["code.repair"],
}

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "write_code_file",
            "description": "在 workspace/scratch 内写入一个有界文本草稿；拒绝绝对路径、路径逃逸、符号链接和可执行/二进制扩展。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "scratch 下相对路径，如 drafts/fix.py。"},
                    "content": {"type": "string", "description": "UTF-8 文本，最多 128 KiB。"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": "已禁用的旧接口。请改用 code.repair 隔离 Runner 运行测试。",
            "parameters": {
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
            },
        },
    },
]

_ALLOWED_SUFFIXES = {
    ".py",
    ".md",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".java",
    ".kt",
    ".kts",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".vue",
    ".html",
    ".css",
    ".sql",
    ".sh",
}
_MAX_BYTES = 131_072
_scratch_root: Path | None = None


def set_project_root(path: str | Path | None) -> None:
    """Compatibility test hook; the supplied directory becomes the scratch root."""
    global _scratch_root
    _scratch_root = None if path is None else Path(path).resolve()


def _get_scratch_root() -> Path:
    if _scratch_root is not None:
        root = _scratch_root
    else:
        configured = os.environ.get("AGENELF_SCRATCH_DIR", "").strip()
        if configured:
            root = Path(configured).resolve()
        else:
            runtime = os.environ.get("AGENELF_ROOT", "").strip()
            base = Path(runtime).resolve() if runtime else Path(__file__).resolve().parents[2]
            root = base / "workspace" / "scratch"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _safe_target(path: str) -> Path:
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path 不能为空")
    raw = Path(path.strip())
    if raw.is_absolute() or ".." in raw.parts:
        raise ValueError("路径逃逸出 scratch 目录")
    if raw.suffix.lower() not in _ALLOWED_SUFFIXES:
        raise ValueError(f"不允许写入扩展名 {raw.suffix or '（无）'}")
    root = _get_scratch_root()
    candidate = root / raw
    resolved_parent = candidate.parent.resolve()
    try:
        resolved_parent.relative_to(root)
    except ValueError as exc:
        raise ValueError("路径逃逸出 scratch 目录") from exc
    cursor = root
    for part in raw.parts[:-1]:
        cursor = cursor / part
        if cursor.exists() and cursor.is_symlink():
            raise ValueError("路径中包含符号链接")
    if candidate.exists() and candidate.is_symlink():
        raise ValueError("目标文件是符号链接")
    return candidate


def write_code_file(path: str, content: str) -> str:
    try:
        target = _safe_target(path)
        text = str(content or "")
        size = len(text.encode("utf-8"))
        if size > _MAX_BYTES:
            return f"写入失败：内容 {size} 字节超过 {_MAX_BYTES} 字节上限"
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{target.name}-", suffix=".tmp", dir=target.parent, text=True
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(tmp_name, target)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return str(target.resolve())
    except (OSError, ValueError) as exc:
        return f"写入失败：{exc}"


def run_python(code: str) -> str:
    del code
    return (
        "执行已拒绝：Agent 进程内任意 Python 执行已永久禁用。"
        "请使用 code.repair，把 unified diff 交给无网络 repair-runner 并运行主人配置的测试。"
    )


_DISPATCH = {
    "write_code_file": lambda args: write_code_file(args.get("path", ""), args.get("content", "")),
    "run_python": lambda args: run_python(args.get("code", "")),
}


def execute(tool_name: str, args: dict) -> str:
    handler = _DISPATCH.get(tool_name)
    if handler is None:
        return f"未知工具：{tool_name}，可用工具：{', '.join(sorted(_DISPATCH))}"
    try:
        return handler(args or {})
    except Exception as exc:
        return f"执行失败：{type(exc).__name__}: {exc}"
