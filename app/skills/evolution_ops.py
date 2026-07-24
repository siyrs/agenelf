"""evolution_ops 技能：自我迭代工作流（对接容器安全机制）。

容器内目录布局（运行时根目录下）：
- app-fork/   agent 运行代码副本（只读，本技能绝不写入）
- app-tmp/    可写暂存区，自我迭代的所有修改都在这里进行
- scripts/    安全脚本（只读，只能通过 subprocess 触发，绝不写入）
- data/       会话与晋升请求数据（evolution-session.json、promote-requests/）
- logs/、workspace/  日志与可写工作区

工作流：
1. evolution_begin(goal)         复制 app-fork/ 全量代码到 app-tmp/，创建会话
2. evolution_write_file(...)     在 app-tmp/ 内修改代码（硬校验禁止逃逸）
3. evolution_run_tests()         在 app-tmp/ 内跑单元测试，更新会话状态
4. evolution_request_promotion() 前置检查通过后触发 scripts/gate_check.sh，
                                 由宿主机完成底线检查与最终晋升
5. evolution_status()            查看当前会话与最近的晋升结果

安全红线：任何文件写入目标必须解析在 app-tmp/ 之内，
app-fork/ 与 scripts/ 一律拒绝（代码级硬校验，不依赖提示词约束）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

SKILL_META = {
    "name": "evolution_ops",
    "description": (
        "自我迭代工作流：把运行代码复制到 app-tmp 暂存区，在其中修改并测试，"
        "通过后触发宿主机安全脚本 gate_check.sh 申请晋升。"
    ),
    "version": "0.1.0",
}

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "evolution_begin",
            "description": "开始一次自我迭代：清空 app-tmp/ 并把 app-fork/ 全量复制进去，在 data/evolution-session.json 创建会话记录，返回中文说明。",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "本次自我迭代的目标描述（例如：修复 chat 中的某某问题）。",
                    },
                },
                "required": ["goal"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "evolution_write_file",
            "description": "往 app-tmp/ 暂存区内写入文件（相对 app-tmp 根的路径），自动创建父目录；拒绝 ../ 逃逸与任何指向 app-fork/、scripts/ 的路径。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "相对 app-tmp/ 根目录的文件路径（如 skills/new_skill.py）。",
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
            "name": "evolution_run_tests",
            "description": "在 app-tmp/ 内运行单元测试（python -m unittest discover，60 秒超时），返回结果摘要并更新会话测试状态。",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "evolution_request_promotion",
            "description": "申请晋升：前置检查（会话存在且测试已通过）后触发 scripts/gate_check.sh；通过则提交晋升请求等待宿主机执行，被拒绝则返回原因。",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "evolution_status",
            "description": "查看当前自我迭代会话状态，以及 data/promote-requests/ 下最近的晋升请求结果。",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
]

# 单元测试子进程超时时间（秒）
_TEST_TIMEOUT = 60
# 安全脚本调用超时时间（秒）
_GATE_TIMEOUT = 120
# 会话记录文件名（位于 data/ 下）
_SESSION_FILENAME = "evolution-session.json"


# ----------------------------------------------------------------------
# 运行时根目录探测
# ----------------------------------------------------------------------
def _get_root() -> Path:
    """获取运行时根目录。

    优先取环境变量 AGENELF_ROOT；否则按本文件位置推断：
    本文件固定位于 <app目录>/skills/evolution_ops.py，
    向上三级即运行时根（本地为项目根，容器内为 /agenelf/）。
    """
    env_root = os.environ.get("AGENELF_ROOT", "").strip()
    if env_root:
        return Path(env_root).resolve()
    return Path(__file__).resolve().parents[2]


def _dir_fork(root: Path) -> Path:
    """app-fork/ 目录（只读代码副本）。"""
    return root / "app-fork"


def _dir_tmp(root: Path) -> Path:
    """app-tmp/ 目录（可写暂存区）。"""
    return root / "app-tmp"


def _dir_scripts(root: Path) -> Path:
    """scripts/ 目录（只读安全脚本）。"""
    return root / "scripts"


def _dir_data(root: Path) -> Path:
    """data/ 目录（会话与晋升请求数据）。"""
    return root / "data"


# ----------------------------------------------------------------------
# 会话记录读写
# ----------------------------------------------------------------------
def _session_path(root: Path) -> Path:
    """会话记录文件路径。"""
    return _dir_data(root) / _SESSION_FILENAME


def _load_session(root: Path) -> dict | None:
    """读取当前会话；不存在或文件损坏时返回 None。"""
    path = _session_path(root)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _save_session(root: Path, session: dict) -> None:
    """写回会话记录，并刷新 updated_at 时间戳。"""
    session["updated_at"] = _now_iso()
    path = _session_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _now_iso() -> str:
    """当前时间的 ISO 8601 字符串（UTC）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _bash_executable() -> str:
    """获取执行安全脚本的 bash；Windows 优先选择可处理本地路径的 Git Bash。"""
    if os.name == "nt":
        candidate = Path(os.environ.get("PROGRAMFILES", r"C:\\Program Files")) / "Git" / "bin" / "bash.exe"
        if candidate.is_file():
            return str(candidate)
    return "bash"


def _bash_script_path(path: Path) -> str:
    """把 Windows 脚本路径转成 Git Bash 可识别的 C:/... 格式。"""
    return path.as_posix() if os.name == "nt" else str(path)


# ----------------------------------------------------------------------
# 工具实现
# ----------------------------------------------------------------------
def evolution_begin(goal: str) -> str:
    """开始一次自我迭代：复制 app-fork/ 到 app-tmp/ 并创建会话。"""
    if not isinstance(goal, str) or not goal.strip():
        return "开始失败：goal 不能为空"
    root = _get_root()
    fork = _dir_fork(root)
    tmp = _dir_tmp(root)
    if not fork.is_dir():
        return f"开始失败：运行代码副本目录不存在：{fork}"

    # 先清空 app-tmp/ 再全量复制，保证暂存区与 app-fork/ 完全一致
    try:
        if tmp.exists():
            shutil.rmtree(tmp)
        shutil.copytree(fork, tmp)
    except OSError as exc:
        return f"开始失败：复制代码副本到暂存区出错：{exc}"

    # 统计复制的文件数，便于在说明中反馈
    file_count = sum(1 for p in tmp.rglob("*") if p.is_file())

    session = {
        "id": f"evo-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}",
        "goal": goal.strip(),
        "started_at": _now_iso(),
        "status": "editing",
        "tests_passed": False,
        "root": str(root),
    }
    try:
        _save_session(root, session)
    except OSError as exc:
        return f"开始失败：无法写入会话记录：{exc}"

    return (
        f"自我迭代会话已开始（ID：{session['id']}）。\n"
        f"目标：{session['goal']}\n"
        f"已把 {fork} 的 {file_count} 个文件复制到暂存区 {tmp}。\n"
        "接下来请用 evolution_write_file 在暂存区内修改代码，"
        "完成后用 evolution_run_tests 运行测试，"
        "测试通过后再用 evolution_request_promotion 申请晋升。"
    )


def evolution_write_file(path: str, content: str) -> str:
    """往 app-tmp/ 内写文件；目标路径硬校验必须落在 app-tmp/ 之内。"""
    if not isinstance(path, str) or not path.strip():
        return "写入失败：path 不能为空"
    if not isinstance(content, str):
        return "写入失败：content 必须是字符串"
    root = _get_root()
    tmp = _dir_tmp(root)
    if not tmp.is_dir():
        return "写入失败：暂存区不存在，请先调用 evolution_begin"

    raw = Path(path)
    # 相对路径拼到 app-tmp 根下；绝对路径保持原样，随后统一做包含校验
    candidate = raw if raw.is_absolute() else tmp / raw
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        return f"写入失败：路径无法解析：{exc}"

    # 安全红线（硬校验）：目标必须位于 app-tmp/ 之内。
    # 这一道校验同时挡住了 ../ 逃逸、绝对路径逃逸，
    # 以及任何指向 app-fork/、scripts/ 等只读目录的写入。
    if not resolved.is_relative_to(tmp.resolve()):
        return (
            f"写入失败：路径 {path!r} 逃逸出暂存区 {tmp}。"
            "安全约束：只允许修改 app-tmp/ 内的文件，"
            "禁止直接修改 app-fork/ 与 scripts/。"
        )

    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
    except OSError as exc:
        return f"写入失败：{exc}"
    return f"已写入暂存区文件：{resolved}（{len(content)} 字符）"


def evolution_run_tests() -> str:
    """在 app-tmp/ 内运行单元测试，返回摘要并更新会话状态。"""
    root = _get_root()
    tmp = _dir_tmp(root)
    if not tmp.is_dir():
        return "测试失败：暂存区不存在，请先调用 evolution_begin"

    session = _load_session(root)
    if session is None:
        return "测试失败：未找到迭代会话，请先调用 evolution_begin"

    tests_dir = tmp / "tests"
    if tests_dir.is_dir():
        # 优先使用 unittest discover 全量发现 tests/ 下的用例。
        # tests/ 是包（含 __init__.py）时加 -t . 以项目根为顶层导入；
        # 否则省略 -t，避免 Python 3.11+ 报 "Start directory is not importable"
        cmd = [sys.executable, "-m", "unittest", "discover", "-s", "tests"]
        if (tests_dir / "__init__.py").exists():
            cmd += ["-t", "."]
    else:
        # 没有 tests/ 目录时退化为逐个运行散落的 test_*.py
        test_files = sorted(tmp.rglob("test_*.py"))
        if not test_files:
            return "测试失败：暂存区内未找到任何测试文件（tests/ 目录或 test_*.py）"
        cmd = [sys.executable, "-m", "unittest"] + [
            str(f.relative_to(tmp)) for f in test_files
        ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TEST_TIMEOUT,
            cwd=str(tmp),
        )
    except subprocess.TimeoutExpired:
        session["status"] = "tests_failed"
        session["tests_passed"] = False
        _save_session(root, session)
        return f"测试超时（{_TEST_TIMEOUT} 秒限制），进程已被终止；会话状态已标记为 tests_failed"
    except OSError as exc:
        return f"测试失败：无法启动测试子进程：{exc}"

    passed = proc.returncode == 0
    session["status"] = "tests_passed" if passed else "tests_failed"
    session["tests_passed"] = passed
    _save_session(root, session)

    # unittest 的统计摘要输出在 stderr，截断过长的输出
    output = (proc.stderr or proc.stdout or "").strip()
    if len(output) > 2000:
        output = output[-2000:]
    verdict = "通过" if passed else "未通过"
    return (
        f"测试{verdict}（退出码 {proc.returncode}）。\n"
        f"命令：{' '.join(cmd)}（工作目录 {tmp}）\n"
        f"输出摘要：\n{output or '（无输出）'}"
    )


def evolution_request_promotion() -> str:
    """申请晋升：前置检查通过后触发 scripts/gate_check.sh 安全脚本。"""
    root = _get_root()
    session = _load_session(root)
    if session is None:
        return "晋升请求被拒绝：未找到迭代会话，请先调用 evolution_begin"
    if session.get("status") == "promotion_requested":
        return (
            f"晋升请求已提交过（会话 {session['id']}），正在等待宿主机执行；"
            "可用 evolution_status 查看进度"
        )
    if not session.get("tests_passed"):
        return (
            "晋升请求被拒绝：测试尚未通过。"
            "请先用 evolution_run_tests 运行测试并确保全部通过，再重新申请"
        )

    gate_script = _dir_scripts(root) / "gate_check.sh"
    if not gate_script.is_file():
        return f"晋升请求被拒绝：安全脚本不存在：{gate_script}"

    try:
        proc = subprocess.run(
            [_bash_executable(), _bash_script_path(gate_script), session["id"]],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GATE_TIMEOUT,
            cwd=str(root),
        )
    except subprocess.TimeoutExpired:
        return f"晋升请求失败：安全脚本执行超时（{_GATE_TIMEOUT} 秒），请检查 scripts/gate_check.sh"
    except OSError as exc:
        return f"晋升请求失败：无法启动安全脚本：{exc}"

    script_output = "\n".join(
        part for part in (proc.stdout.strip(), proc.stderr.strip()) if part
    )

    if proc.returncode == 0:
        session["status"] = "promotion_requested"
        _save_session(root, session)
        return (
            f"底线检查通过，晋升请求已提交（会话 {session['id']}），"
            "等待宿主机执行最终晋升。\n"
            f"安全脚本输出：\n{script_output or '（无输出）'}"
        )

    session["status"] = "promotion_rejected"
    _save_session(root, session)
    return (
        f"晋升请求被安全脚本拒绝（退出码 {proc.returncode}）。\n"
        f"原因：\n{script_output or '（脚本未给出原因）'}\n"
        "请根据上述原因在 app-tmp/ 内修正，重新运行测试通过后再次触发本工具。"
    )


def evolution_status() -> str:
    """返回当前会话状态与最近的晋升请求结果。"""
    root = _get_root()
    lines: list[str] = []

    session = _load_session(root)
    if session is None:
        lines.append("当前没有进行中的自我迭代会话。")
    else:
        lines.append(
            "当前会话：\n"
            f"  ID：{session.get('id')}\n"
            f"  目标：{session.get('goal')}\n"
            f"  状态：{session.get('status')}\n"
            f"  测试通过：{'是' if session.get('tests_passed') else '否'}\n"
            f"  开始时间：{session.get('started_at')}\n"
            f"  最近更新：{session.get('updated_at', '未知')}"
        )

    # 扫描 data/promote-requests/ 下最近的晋升请求（按目录修改时间倒序，最多 5 条）
    requests_dir = _dir_data(root) / "promote-requests"
    if requests_dir.is_dir():
        entries = [p for p in requests_dir.iterdir() if p.is_dir()]
        entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        if entries:
            lines.append("最近的晋升请求：")
            for entry in entries[:5]:
                markers = sorted(f.name for f in entry.iterdir() if f.is_file())
                lines.append(
                    f"  - {entry.name}：标记文件 {', '.join(markers) or '（无）'}"
                )
        else:
            lines.append("暂无晋升请求记录。")
    else:
        lines.append("暂无晋升请求记录。")

    return "\n".join(lines)


_DISPATCH = {
    "evolution_begin": lambda a: evolution_begin(a.get("goal", "")),
    "evolution_write_file": lambda a: evolution_write_file(
        a.get("path", ""), a.get("content", "")
    ),
    "evolution_run_tests": lambda a: evolution_run_tests(),
    "evolution_request_promotion": lambda a: evolution_request_promotion(),
    "evolution_status": lambda a: evolution_status(),
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
