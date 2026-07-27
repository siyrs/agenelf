"""Controlled self-evolution tools backed by a repository-shaped candidate workspace.

The runtime code remains read-only. Every candidate is staged inside ``app-tmp`` and
is visible to the host gate through a bind mount. Existing tests are immutable:
a candidate may add a new ``tests/test_*.py`` file, but it cannot modify or delete any
trusted baseline test to make a broken implementation appear green.
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from core.evolution_workspace import (
    EvolutionWorkspaceError,
    assert_trusted_tests_unchanged,
    baseline_test_manifest,
    candidate_app,
    candidate_path,
    clear_tree_contents,
    stage_workspace,
    validate_relative_app_path,
)

SKILL_META = {
    "name": "evolution_ops",
    "description": (
        "受控自我迭代：建立完整仓库候选、先验证可信基线、仅允许新增测试，"
        "通过独立测试 Runner 与宿主机 gate 后申请晋升。"
    ),
    "version": "0.2.0",
}

CAPABILITY_META = {
    "id": "agent.evolution",
    "name": "受控自我迭代",
    "description": (
        "在 app-tmp 的宿主机可见候选区中进行有限代码修改；基线失败、测试篡改和"
        "宿主机控制面变更都会失败关闭。"
    ),
    "version": "0.2.0",
    "domain": "orchestration",
    "operations": [
        {"name": "evolution_begin", "description": "建立候选并执行基线预检", "risk": "change"},
        {"name": "evolution_write_file", "description": "写入受限候选文件", "risk": "change"},
        {"name": "evolution_run_tests", "description": "运行可信基线与新增测试", "risk": "change"},
        {"name": "evolution_request_promotion", "description": "申请宿主机门禁晋升", "risk": "change"},
        {"name": "evolution_status", "description": "查看候选状态与证据", "risk": "read"},
    ],
    "composes_with": [
        "agent.self_development",
        "agent.task_continuation",
        "software.validation",
    ],
}

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "evolution_begin",
            "description": (
                "开始一次受控自我迭代。会清空 app-tmp 的内容但保留挂载点，建立完整候选仓库，"
                "并在请求模型生成补丁前执行可信基线测试。基线失败时立即阻断。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"goal": {"type": "string"}},
                "required": ["goal"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "evolution_write_file",
            "description": (
                "写入候选 app 的 core/、skills/ 或新增 tests/test_*.py。"
                "既有测试、安全关键模块和候选区之外的路径不可修改。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "evolution_run_tests",
            "description": (
                "分别运行不可变的可信基线测试和候选新增测试；任何既有测试变化都会直接拒绝。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "evolution_request_promotion",
            "description": (
                "测试通过后运行只读宿主机安全门禁并提交精确候选摘要；不会自行合并主分支。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "evolution_status",
            "description": "查看当前候选布局、基线预检、测试状态和最近晋升请求。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]

_TEST_TIMEOUT = 420
_GATE_TIMEOUT = 300
_SESSION_FILENAME = "evolution-session.json"
_MAX_FILE_CHARS = 50_000
_ALLOWED_PREFIXES = ("core/", "skills/", "tests/")
_PROTECTED_APP_PATHS = frozenset(
    {
        "core/autonomy.py",
        "core/capabilities.py",
        "core/capability_health.py",
        "core/configuration.py",
        "core/continuous_chat.py",
        "core/evolution_workspace.py",
        "core/execution_policy.py",
        "core/local_context.py",
        "core/memory.py",
        "core/operations.py",
        "core/permissions.py",
        "core/policy.py",
        "core/privacy.py",
        "core/reasoning_trace.py",
        "core/registry.py",
        "core/self_development.py",
        "core/validation.py",
        "skills/evolution_ops.py",
        "skills/evolution_scope_guard.py",
        "skills/server_ops.py",
        "skills/compose_lifecycle.py",
        "skills/zz_transport_resilience.py",
    }
)


def _root() -> Path:
    configured = os.environ.get("AGENELF_ROOT", "").strip()
    return Path(configured).resolve() if configured else Path(__file__).resolve().parents[2]


def _source_app(root: Path) -> Path:
    # The Compose runtime maps the current app/ source at the historical app-fork path.
    for path in (root / "app-fork", root / "app"):
        if path.is_dir():
            return path.resolve()
    return root / "app-fork"


def _data(root: Path) -> Path:
    return root / "data"


def _promotion_request_dirs(root: Path) -> list[tuple[str, Path]]:
    """Promote-request sources: gate candidate output first, host-promoted archive second.

    gate_check.sh 默认把候选晋升请求写入 ``app-tmp/promote-requests``
    （可用 ``PROMOTE_REQUESTS_DIR`` 覆盖）；promote.sh 校验通过后才由宿主机
    移入 ``data/promote-requests``。读取方需合并两个目录。
    """
    configured = os.environ.get("PROMOTE_REQUESTS_DIR", "").strip()
    candidate = (
        Path(configured).resolve()
        if configured
        else root / "app-tmp" / "promote-requests"
    )
    return [
        ("candidate", candidate),
        ("promoted", _data(root) / "promote-requests"),
    ]


def merged_promotion_requests(root: Path, limit: int | None = None) -> list[dict[str, Any]]:
    """合并 app-tmp 候选区与 data 晋升区的请求目录（目录缺失误差容错）。

    同 ID 同时存在时以宿主机已晋升（promoted）记录为准。返回条目含
    ``id``/``source``（``candidate`` 或 ``promoted``）/``markers``，按修改时间倒序。
    """
    merged: dict[str, dict[str, Any]] = {}
    for source, directory in _promotion_request_dirs(root):
        if not directory.is_dir():
            continue
        try:
            entries = [item for item in directory.iterdir() if item.is_dir()]
        except OSError:
            continue
        for entry in entries:
            try:
                markers = sorted(
                    path.name for path in entry.iterdir() if path.is_file()
                )
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            existing = merged.get(entry.name)
            if existing is not None and existing["source"] == "promoted":
                continue
            merged[entry.name] = {
                "id": entry.name,
                "source": source,
                "markers": markers,
                "mtime": mtime,
            }
    rows = sorted(merged.values(), key=lambda item: float(item["mtime"]), reverse=True)
    if limit is not None:
        rows = rows[:limit]
    for row in rows:
        row.pop("mtime", None)
    return rows


def _session_path(root: Path) -> Path:
    return _data(root) / _SESSION_FILENAME


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=".tmp", dir=path.parent, text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _load_session(root: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(_session_path(root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _save_session(root: Path, session: dict[str, Any]) -> None:
    session["updated_at"] = _now()
    _atomic_json(_session_path(root), session)


def _archive_session(root: Path, session: dict[str, Any]) -> None:
    session_id = re.sub(r"[^A-Za-z0-9._-]", "_", str(session.get("id", "unknown")))
    _atomic_json(_data(root) / "evolution-sessions" / f"{session_id}.json", session)


@contextmanager
def _workspace_lock(root: Path) -> Iterator[None]:
    lock = _data(root) / "evolution.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists():
        try:
            stale = time.time() - lock.stat().st_mtime > 600
        except OSError:
            stale = False
        if stale:
            lock.unlink(missing_ok=True)
    try:
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise EvolutionWorkspaceError(
            "已有自我迭代正在准备候选，请先查看 evolution_status"
        ) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"pid={os.getpid()} at={_now()}\n")
        yield
    finally:
        lock.unlink(missing_ok=True)


def _bash() -> str:
    if os.name == "nt":
        candidate = (
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
            / "Git"
            / "bin"
            / "bash.exe"
        )
        if candidate.is_file():
            return str(candidate)
    return "bash"


def _safe_output(process: subprocess.CompletedProcess[str]) -> str:
    output = "\n".join(
        part.strip() for part in (process.stdout, process.stderr) if part and part.strip()
    )
    return output[-8000:] if len(output) > 8000 else output


def _fallback_unittest(candidate: Path) -> tuple[int, str]:
    command = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(candidate), str(candidate.parent), environment.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    try:
        process = subprocess.run(
            command,
            cwd=candidate,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TEST_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return 124, f"测试超时（{_TEST_TIMEOUT}s）"
    except OSError as exc:
        return 125, f"无法启动测试：{type(exc).__name__}: {exc}"
    return process.returncode, _safe_output(process)


def _run_candidate_tests(
    root: Path, session: dict[str, Any], phase: str
) -> tuple[int, str]:
    script = root / "scripts" / "run_candidate_tests.py"
    candidate = Path(str(session["candidate_app"]))
    baseline = _source_app(root)
    if not script.is_file():
        # Isolated legacy unit tests may intentionally provide only a tiny gate stub.
        return _fallback_unittest(candidate)
    command = [
        sys.executable,
        str(script),
        "--baseline",
        str(baseline),
        "--candidate",
        str(candidate),
        "--phase",
        phase,
        "--timeout",
        "300",
    ]
    try:
        process = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TEST_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return 124, f"候选测试 Runner 超时（{_TEST_TIMEOUT}s）"
    except OSError as exc:
        return 125, f"候选测试 Runner 无法启动：{type(exc).__name__}: {exc}"
    return process.returncode, _safe_output(process)


def evolution_begin(goal: str) -> str:
    goal = str(goal or "").strip()
    if not goal:
        return "开始失败：goal 不能为空"
    root = _root()
    source = _source_app(root)
    if not source.is_dir():
        return f"开始失败：运行代码基线不存在：{source}"

    try:
        with _workspace_lock(root):
            previous = _load_session(root)
            if previous:
                _archive_session(root, previous)
            marker = stage_workspace(root, source)
            session: dict[str, Any] = {
                "schema_version": 2,
                "id": (
                    f"evo-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-"
                    f"{uuid.uuid4().hex[:6]}"
                ),
                "goal": goal,
                "started_at": _now(),
                "updated_at": _now(),
                "status": "baseline_checking",
                "tests_passed": False,
                "layout": marker["layout"],
                "candidate_repo": marker["candidate_repo"],
                "candidate_app": marker["candidate_app"],
                "baseline_tests": marker["baseline_tests"],
                "changed_files": [],
            }
            _save_session(root, session)
            code, output = _run_candidate_tests(root, session, "baseline")
            session["baseline_preflight"] = {
                "exit_code": code,
                "output": output,
                "checked_at": _now(),
            }
            if code != 0:
                session["status"] = "baseline_failed"
                session["error"] = (
                    "可信基线或仓库快照未通过。禁止让模型修改既有测试、CI 或策略来掩盖失败。"
                )
                _save_session(root, session)
                return (
                    f"自我迭代已阻断（ID：{session['id']}）：基线预检失败。\n"
                    "这属于运行环境或当前仓库基线问题，不会进入模型补丁阶段，"
                    "也不会修改测试绕过门禁。\n"
                    f"候选布局：{session['layout']}\n输出：\n{output or '（无输出）'}"
                )
            session["status"] = "editing"
            _save_session(root, session)
    except (EvolutionWorkspaceError, OSError) as exc:
        return f"开始失败：{type(exc).__name__}: {exc}"

    return (
        f"自我迭代会话已开始（ID：{session['id']}）。\n"
        f"目标：{goal}\n候选布局：{session['layout']}\n"
        f"候选 app：{session['candidate_app']}\n"
        f"可信基线测试：{len(session['baseline_tests'])} 个文件，预检通过。\n"
        "候选只能修改非保护代码并新增 tests/test_*.py；既有测试不可覆盖。"
    )


def evolution_write_file(path: str, content: str) -> str:
    root = _root()
    session = _load_session(root)
    if session is None:
        return "写入失败：没有自我迭代会话，请先调用 evolution_begin"
    if session.get("status") not in {"editing", "tests_failed", "new_tests_failed"}:
        return f"写入失败：当前会话状态 {session.get('status')} 不允许修改候选"
    if not isinstance(content, str):
        return "写入失败：content 必须是字符串"
    if len(content) > _MAX_FILE_CHARS:
        return f"写入失败：文件超过 {_MAX_FILE_CHARS} 字符上限"

    try:
        relative = validate_relative_app_path(path)
        if relative in _PROTECTED_APP_PATHS:
            raise EvolutionWorkspaceError(
                f"安全关键模块只能由人类主导仓库变更修改：{relative}"
            )
        if relative.startswith("tests/"):
            if not Path(relative).name.startswith("test_"):
                raise EvolutionWorkspaceError("自主候选只能新增 tests/test_*.py")
            baseline = session.get("baseline_tests", {})
            if isinstance(baseline, dict) and relative in baseline:
                raise EvolutionWorkspaceError(
                    f"既有测试受保护，不能通过修改测试绕过门禁：{relative}"
                )
        ast.parse(content, filename=relative)
        destination = candidate_path(root, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        safe = content.replace("\ud800", "\\ud800").replace("\udfff", "\\udfff")
        destination.write_text(
            safe if safe.endswith("\n") else safe + "\n", encoding="utf-8"
        )
        manifest = session.get("baseline_tests", {})
        if isinstance(manifest, dict):
            assert_trusted_tests_unchanged(candidate_app(root), manifest)
    except (EvolutionWorkspaceError, SyntaxError, OSError) as exc:
        return f"写入失败：{exc}"

    changed = {str(item) for item in session.get("changed_files", [])}
    changed.add(relative)
    session["changed_files"] = sorted(changed)
    session["status"] = "editing"
    session["tests_passed"] = False
    session.pop("test_result", None)
    _save_session(root, session)
    return f"已写入受控候选：{relative}（{len(content)} 字符）"


def evolution_run_tests() -> str:
    root = _root()
    session = _load_session(root)
    if session is None:
        return "测试失败：没有自我迭代会话，请先调用 evolution_begin"
    try:
        manifest = session.get("baseline_tests", {})
        if not isinstance(manifest, dict):
            manifest = baseline_test_manifest(_source_app(root))
        assert_trusted_tests_unchanged(candidate_app(root), manifest)
    except (EvolutionWorkspaceError, OSError) as exc:
        session["status"] = "tests_tampered"
        session["tests_passed"] = False
        session["error"] = str(exc)
        _save_session(root, session)
        return f"测试拒绝：{exc}"

    code, output = _run_candidate_tests(root, session, "candidate")
    passed = code == 0
    session["status"] = "tests_passed" if passed else "tests_failed"
    session["tests_passed"] = passed
    session["test_result"] = {
        "exit_code": code,
        "output": output,
        "finished_at": _now(),
    }
    _save_session(root, session)
    verdict = "通过" if passed else "未通过"
    guidance = (
        ""
        if passed
        else "\n请修复候选实现或新增测试；既有测试、CI、策略和 gate 不属于可绕过对象。"
    )
    return (
        f"候选测试{verdict}（退出码 {code}）。\n"
        f"输出：\n{output or '（无输出）'}{guidance}"
    )


def evolution_request_promotion() -> str:
    root = _root()
    session = _load_session(root)
    if session is None:
        return "晋升请求被拒绝：没有自我迭代会话"
    if session.get("status") == "promotion_requested":
        return f"晋升请求已提交过（会话 {session.get('id')}），请查看 evolution_status"
    if not session.get("tests_passed"):
        return (
            "晋升请求被拒绝：测试尚未通过"
            "（可信基线与候选新增测试必须全部通过）"
        )

    gate = root / "scripts" / "gate_check.sh"
    if not gate.is_file():
        return f"晋升请求被拒绝：安全脚本不存在：{gate}"
    try:
        process = subprocess.run(
            [
                _bash(),
                gate.as_posix() if os.name == "nt" else str(gate),
                str(session["id"]),
            ],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GATE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"晋升请求失败：gate 超时（{_GATE_TIMEOUT}s）"
    except OSError as exc:
        return f"晋升请求失败：无法启动 gate：{type(exc).__name__}: {exc}"

    output = _safe_output(process)
    if process.returncode == 0:
        session["status"] = "promotion_requested"
        session["promotion_output"] = output
        _save_session(root, session)
        return (
            f"底线检查通过，晋升请求已提交（会话 {session['id']}）。\n"
            "候选摘要已绑定；仍需宿主机控制面完成最终晋升。\n"
            f"输出：\n{output or '（无输出）'}"
        )
    session["status"] = "promotion_rejected"
    session["promotion_output"] = output
    _save_session(root, session)
    return (
        f"晋升请求被 gate 拒绝（退出码 {process.returncode}）。\n"
        f"输出：\n{output or '（无输出）'}\n"
        "不得修改既有测试、CI、安全策略或 gate 来消除失败。"
    )


def evolution_status() -> str:
    root = _root()
    session = _load_session(root)
    lines: list[str] = []
    if session is None:
        lines.append("当前没有进行中的自我迭代会话。")
    else:
        baseline = session.get("baseline_preflight", {})
        lines.append(
            "当前会话：\n"
            f"  ID：{session.get('id')}\n"
            f"  目标：{session.get('goal')}\n"
            f"  状态：{session.get('status')}\n"
            f"  布局：{session.get('layout')}\n"
            f"  候选 app：{session.get('candidate_app')}\n"
            f"  基线预检：{'通过' if baseline.get('exit_code') == 0 else '未通过'}\n"
            f"  测试通过：{'是' if session.get('tests_passed') else '否'}\n"
            f"  变更文件：{', '.join(session.get('changed_files', [])) or '（无）'}\n"
            f"  最近更新：{session.get('updated_at')}"
        )
    entries = merged_promotion_requests(root, limit=5)
    if entries:
        lines.append("最近晋升请求：")
        for entry in entries:
            markers = ", ".join(entry["markers"]) or "（无标记）"
            lines.append(f"  - {entry['id']}（{entry['source']}）：{markers}")
    else:
        lines.append("暂无晋升请求记录。")
    return "\n".join(lines)


_DISPATCH = {
    "evolution_begin": lambda args: evolution_begin(args.get("goal", "")),
    "evolution_write_file": lambda args: evolution_write_file(
        args.get("path", ""), args.get("content", "")
    ),
    "evolution_run_tests": lambda args: evolution_run_tests(),
    "evolution_request_promotion": lambda args: evolution_request_promotion(),
    "evolution_status": lambda args: evolution_status(),
}


def execute(tool_name: str, args: dict[str, Any]) -> str:
    handler = _DISPATCH.get(tool_name)
    if handler is None:
        return f"未知工具：{tool_name}，可用工具：{', '.join(sorted(_DISPATCH))}"
    try:
        return str(handler(args or {}))
    except Exception as exc:  # protocol boundary: never crash the chat loop
        return f"执行失败：{type(exc).__name__}: {exc}"
