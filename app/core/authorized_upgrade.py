"""Two-stage owner-authorized self-upgrade engine.

Normal self-evolution remains available for low-risk application changes.  When an
upgrade needs protected runtime, runner, policy, Compose or CI files, the Agent can
still prepare and apply the change, but only through two exact owner decisions:

1. an intent decision bound to the goal, scopes, allowed paths and size limits;
2. a candidate decision bound to the tested tree digest and exact changed-file hashes.

The Agent writes only to ``app-tmp/repo``.  A deterministic, network-isolated runner
applies an approved candidate to explicitly mounted repository paths.  Secrets,
owner-local state, audit evidence and Git metadata are never part of the candidate or
target mount set.
"""
from __future__ import annotations

import ast
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

from core import permissions
from core.evolution_workspace import (
    candidate_repo,
    file_sha256,
    stage_workspace,
)
from core.privacy import redact_sensitive_text

SCHEMA_VERSION = 1
SESSION_PREFIX = "upgrade-"
REQUEST_PREFIX = "self-upgrade-"
DEFAULT_MAX_FILES = 8
DEFAULT_MAX_CHANGED_LINES = 1200
MAX_CONTEXT_CHARS = 220_000
MAX_FILE_CHARS = 120_000
_ALLOWED_SUFFIXES = {".py", ".sh", ".ps1", ".yaml", ".yml", ".json", ".toml", ".md", ".txt"}
_ALLOWED_BASENAMES = {
    "Dockerfile",
    "Makefile",
    "README.md",
    "docker-compose.yml",
    ".env.example",
    ".ops-runner.env.example",
    ".gitignore",
    ".gitleaks.toml",
}

# Scopes expand only to repository paths that the deterministic promotion runner can
# see.  The owner approves the expanded path set, not merely a vague category name.
_SCOPE_PATHS: dict[str, tuple[str, ...]] = {
    "app_runtime": ("app/core/",),
    "skills": ("app/skills/",),
    "tests": ("app/tests/",),
    "runners": ("scripts/",),
    "policy": ("policy/",),
    "compose": ("docker-compose.yml", "Dockerfile", ".env.example", ".ops-runner.env.example"),
    "ci": (".github/workflows/", ".gitleaks.toml"),
    "docs": ("docs/", "README.md", "Makefile"),
    "authorization_control": (
        "app/core/owner_approval.py",
        "app/core/permissions.py",
        "app/core/cli_approval.py",
        "scripts/approval_runner.py",
        "scripts/approve.py",
        "scripts/approve.ps1",
        "scripts/approve.sh",
        "scripts/init_approval_key.py",
    ),
}

# These locations are never candidate or target paths.  Owner authorization cannot
# turn model-visible code into access to credentials, decisions, audit evidence or Git.
_PERMANENTLY_FORBIDDEN_PREFIXES = (
    ".git/",
    "local/",
    "data/",
    "logs/",
    "workspace/",
    "app-tmp/",
    "app-space/",
    "code-workspaces/",
    "repair-space/",
    "secrets/",
)
_PERMANENTLY_FORBIDDEN_EXACT = {
    ".env",
    ".ops-runner.env",
}

_SCOPE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("authorization_control", re.compile(r"(?i)审批|授权|authorization|approval|auth-decisions|approval[_ -]?runner")),
    ("runners", re.compile(r"(?i)\b(?:ops|approval|validation|repair|self[-_ ]?upgrade)[-_ ]?runner\b|执行器|runner")),
    ("compose", re.compile(r"(?i)docker-compose\.ya?ml|docker\s+compose|compose\s+拓扑|挂载(?:点|目录)|network_mode|docker\s+down")),
    ("policy", re.compile(r"(?i)安全策略|权限策略|policy|execution[_ -]?policy|治理规则")),
    ("ci", re.compile(r"(?i)\.github/workflows|github actions|codeql|供应链|\bCI\b")),
    ("app_runtime", re.compile(r"(?i)核心运行时|core/|registry|continuous_chat|reasoning|自我迭代|自主进化")),
    ("skills", re.compile(r"(?i)技能|skill|能力")),
    ("docs", re.compile(r"(?i)文档|README|docs/")),
)

# Content redlines are intentionally semantic as well as path-based.  A protected
# module may be upgraded after authorization, but the candidate may not add common
# self-approval, secret-exfiltration, gate-bypass or arbitrary-shell patterns.
_REDLINE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Docker Socket", re.compile(r"/var/run/docker\.sock|docker\.sock", re.I)),
    ("凭据读取", re.compile(r"local/secrets|AGENELF_APPROVAL_KEY|/agenelf/approval/key|auth-decisions.*(?:write|open\([^)]*['\"]w)", re.I)),
    ("自我批准", re.compile(r"self[_ -]?approve|自动批准|伪造授权|forge[_ -]?owner|decision\s*=\s*['\"]approve['\"]", re.I)),
    ("审计破坏", re.compile(r"(?:unlink|remove|rmtree|truncate)[^\n]{0,120}(?:audit|auth-decisions|promotion-history)", re.I)),
    ("测试或门禁绕过", re.compile(r"monkey.?patch[^\n]{0,120}(?:test|gate|policy)|disable[^\n]{0,80}(?:test|gate|audit|policy)|skip[^\n]{0,80}(?:governance|security)", re.I)),
    ("危险远程脚本", re.compile(r"(?:curl|wget)[^\n|]{0,200}\|\s*(?:sudo\s+)?(?:ba)?sh\b", re.I)),
    ("直接主分支发布", re.compile(r"git[^\n]{0,120}(?:push|merge)[^\n]{0,120}\bmain\b", re.I)),
    ("明显 API Key", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
)

_TEXT_ENCODINGS = ("utf-8",)


class AuthorizedUpgradeError(RuntimeError):
    """Expected, user-visible failure in the authorized upgrade workflow."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def runtime_root(root: str | Path | None = None) -> Path:
    if root is not None:
        return Path(root).resolve()
    configured = os.environ.get("AGENELF_ROOT", "").strip()
    return Path(configured).resolve() if configured else Path(__file__).resolve().parents[2]


def data_paths(root: str | Path | None = None) -> dict[str, Path]:
    base = runtime_root(root)
    data = base / "data"
    return {
        "sessions": data / "authorized-upgrades",
        "requests": data / "self-upgrade-requests",
        "results": data / "self-upgrade-results",
        "locks": data / "self-upgrade-locks",
    }


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _atomic_json(path: Path, value: dict[str, Any], *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if exclusive:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        return
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256_bytes(encoded)


def _normalized_goal(goal: object) -> str:
    return " ".join(str(goal or "").strip().split())[:4000]


def classify_scopes(goal: object, hints: Iterable[str] | None = None) -> list[str]:
    text = _normalized_goal(goal)
    scopes = {str(item).strip() for item in (hints or []) if str(item).strip() in _SCOPE_PATHS}
    for scope, pattern in _SCOPE_PATTERNS:
        if pattern.search(text):
            scopes.add(scope)
    if not scopes:
        scopes.update({"app_runtime", "skills"})
    # Every production-code change needs tests, but existing tests remain immutable.
    if scopes - {"docs"}:
        scopes.add("tests")
    return sorted(scopes)


def expand_allowed_paths(scopes: Iterable[str]) -> list[str]:
    values: set[str] = set()
    for scope in scopes:
        if scope not in _SCOPE_PATHS:
            raise AuthorizedUpgradeError(f"未知升级范围：{scope}")
        values.update(_SCOPE_PATHS[scope])
    return sorted(values)


def _path_forbidden(path: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    return normalized in _PERMANENTLY_FORBIDDEN_EXACT or any(
        normalized == prefix.rstrip("/") or normalized.startswith(prefix)
        for prefix in _PERMANENTLY_FORBIDDEN_PREFIXES
    )


def _path_matches(path: str, allowed: Iterable[str]) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    if _path_forbidden(normalized):
        return False
    for rule in allowed:
        rule = str(rule).replace("\\", "/").lstrip("./")
        if rule.endswith("/") and normalized.startswith(rule):
            return True
        if normalized == rule:
            return True
    return False


def validate_repo_path(path: object, allowed_paths: Iterable[str]) -> str:
    raw = str(path or "").replace("\\", "/").strip()
    if not raw or raw.startswith("/"):
        raise AuthorizedUpgradeError("升级文件必须是仓库相对路径")
    parts = [item for item in raw.split("/") if item not in {"", "."}]
    if not parts or any(item == ".." for item in parts):
        raise AuthorizedUpgradeError(f"升级路径逃逸：{path!r}")
    normalized = "/".join(parts)
    if _path_forbidden(normalized):
        raise AuthorizedUpgradeError(f"路径属于永久红线，任何授权都不能覆盖：{normalized}")
    if not _path_matches(normalized, allowed_paths):
        raise AuthorizedUpgradeError(f"路径超出主人批准范围：{normalized}")
    name = Path(normalized).name
    suffix = Path(normalized).suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES and name not in _ALLOWED_BASENAMES:
        raise AuthorizedUpgradeError(f"不支持的升级文件类型：{normalized}")
    return normalized


def scan_redlines(path: str, content: str) -> None:
    for label, pattern in _REDLINE_PATTERNS:
        if pattern.search(content):
            raise AuthorizedUpgradeError(f"候选 {path} 命中永久安全红线：{label}")


def make_plan(
    goal: object,
    scopes: Iterable[str] | None = None,
    *,
    max_files: int = DEFAULT_MAX_FILES,
    max_changed_lines: int = DEFAULT_MAX_CHANGED_LINES,
) -> dict[str, Any]:
    normalized_goal = _normalized_goal(goal)
    if not normalized_goal:
        raise AuthorizedUpgradeError("升级目标不能为空")
    selected_scopes = classify_scopes(normalized_goal, scopes)
    bounded_files = max(1, min(int(max_files), 20))
    bounded_lines = max(50, min(int(max_changed_lines), 4000))
    plan = {
        "schema_version": SCHEMA_VERSION,
        "kind": "owner_authorized_self_upgrade_intent",
        "goal": normalized_goal,
        "goal_sha256": _sha256_bytes(normalized_goal.encode("utf-8")),
        "scopes": selected_scopes,
        "allowed_paths": expand_allowed_paths(selected_scopes),
        "max_files": bounded_files,
        "max_changed_lines": bounded_lines,
        "requires_candidate_approval": True,
        "redline_policy": "owner-authorized-upgrade-v1",
    }
    plan["fingerprint"] = _json_digest(plan)
    return plan


def _session_path(session_id: str, root: str | Path | None = None) -> Path:
    if not re.fullmatch(r"upgrade-[0-9]{8}-[0-9]{6}-[0-9a-f]{8}", str(session_id or "")):
        raise AuthorizedUpgradeError(f"非法升级会话 ID：{session_id!r}")
    return data_paths(root)["sessions"] / f"{session_id}.json"


def load_session(session_id: str, root: str | Path | None = None) -> dict[str, Any]:
    value = _read_json(_session_path(session_id, root))
    if value is None:
        raise AuthorizedUpgradeError(f"升级会话不存在：{session_id}")
    return value


def save_session(session: dict[str, Any], root: str | Path | None = None) -> None:
    session["updated_at"] = now_iso()
    _atomic_json(_session_path(str(session["id"]), root), session)


def list_sessions(root: str | Path | None = None, limit: int = 20) -> list[dict[str, Any]]:
    directory = data_paths(root)["sessions"]
    if not directory.is_dir():
        return []
    paths = sorted(directory.glob("upgrade-*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    return [value for path in paths[: max(0, min(int(limit), 100))] if (value := _read_json(path))]


def find_session_for_plan(plan: dict[str, Any], root: str | Path | None = None) -> dict[str, Any] | None:
    fingerprint = str(plan.get("fingerprint", ""))
    for session in list_sessions(root, limit=100):
        if session.get("plan_fingerprint") == fingerprint and session.get("status") not in {
            "denied",
            "expired",
            "failed",
            "rolled_back",
        }:
            return session
    return None


def _request_intent(plan: dict[str, Any]) -> str:
    binding = {key: value for key, value in plan.items() if key != "fingerprint"}
    ok, request_id = permissions.request_auth(
        "authorized_self_upgrade",
        "authorize_upgrade_intent",
        f"允许 Agenelf 按精确范围升级：{plan['goal']}",
        reason="主人确认升级意图、路径范围和规模上限后才允许生成候选",
        ttl_seconds=900,
        binding=binding,
        operation="owner_authorized_self_upgrade_intent",
        capability="agent.authorized_self_upgrade",
        channel="cli",
    )
    if not ok:
        raise AuthorizedUpgradeError(request_id)
    return request_id


def create_or_get_session(
    goal: object,
    scopes: Iterable[str] | None = None,
    *,
    max_files: int = DEFAULT_MAX_FILES,
    max_changed_lines: int = DEFAULT_MAX_CHANGED_LINES,
    root: str | Path | None = None,
) -> dict[str, Any]:
    plan = make_plan(goal, scopes, max_files=max_files, max_changed_lines=max_changed_lines)
    existing = find_session_for_plan(plan, root)
    if existing is not None:
        return existing
    session_id = f"upgrade-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    intent_auth_id = _request_intent(plan)
    session = {
        "schema_version": SCHEMA_VERSION,
        "id": session_id,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "status": "awaiting_intent_approval",
        "goal": plan["goal"],
        "plan": plan,
        "plan_fingerprint": plan["fingerprint"],
        "intent_auth_id": intent_auth_id,
        "events": [
            {
                "at": now_iso(),
                "phase": "intent",
                "detail": f"已创建精确升级意图授权 {intent_auth_id}",
            }
        ],
    }
    save_session(session, root)
    return session


def _event(session: dict[str, Any], phase: str, detail: object) -> None:
    safe = redact_sensitive_text(str(detail or ""))[-4000:]
    session.setdefault("events", []).append({"at": now_iso(), "phase": phase, "detail": safe})


def _tree_manifest(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if any(part in {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"} for part in relative.parts):
            continue
        if path.suffix in {".pyc", ".pyo"} or path.name == ".agenelf-evolution-workspace.json":
            continue
        result[relative.as_posix()] = file_sha256(path)
    return result


def _tree_digest(root: Path) -> str:
    return _json_digest(_tree_manifest(root))


def _manifest_path(session_id: str, name: str, root: Path) -> Path:
    return data_paths(root)["sessions"] / session_id / name


def _write_manifest_file(session_id: str, name: str, value: Any, root: Path) -> Path:
    path = _manifest_path(session_id, name, root)
    _atomic_json(path, value)
    return path


def _read_text(path: Path) -> str | None:
    if path.stat().st_size > MAX_FILE_CHARS:
        return None
    for encoding in _TEXT_ENCODINGS:
        try:
            return path.read_text(encoding=encoding)
        except (OSError, UnicodeDecodeError):
            continue
    return None


def _goal_tokens(goal: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z_][A-Za-z0-9_.-]{2,}|[\u4e00-\u9fff]{2,}", goal)
        if len(token) >= 2
    }


def _candidate_context(repo: Path, allowed_paths: list[str], goal: str) -> dict[str, str]:
    candidates: list[tuple[int, int, str, str]] = []
    tokens = _goal_tokens(goal)
    for path in sorted(repo.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(repo).as_posix()
        if not _path_matches(relative, allowed_paths):
            continue
        if path.suffix.lower() not in _ALLOWED_SUFFIXES and path.name not in _ALLOWED_BASENAMES:
            continue
        body = _read_text(path)
        if body is None:
            continue
        lowered = relative.lower()
        score = sum(5 for token in tokens if token.lower() in lowered)
        if relative.startswith("app/skills/"):
            score += 2
        if relative.startswith("app/core/"):
            score += 1
        candidates.append((-score, len(body), relative, body))
    result: dict[str, str] = {}
    used = 0
    for _score, _size, relative, body in sorted(candidates):
        addition = len(relative) + len(body)
        if result and used + addition > MAX_CONTEXT_CHARS:
            continue
        result[relative] = body
        used += addition
        if used >= MAX_CONTEXT_CHARS:
            break
    return result


def _parse_file_blocks(content: object) -> dict[str, str]:
    text = str(content or "")
    changes: dict[str, str] = {}
    for match in re.finditer(r"```[^\n]*\n(.*?)```", text, re.DOTALL):
        block = match.group(1)
        lines = block.splitlines()
        if not lines:
            continue
        header = re.match(r"#\s*FILE\s*[:：]\s*(\S+)", lines[0].strip(), re.I)
        if not header:
            continue
        path = header.group(1).replace("\\", "/")
        body = "\n".join(lines[1:])
        if not body.endswith("\n"):
            body += "\n"
        changes[path] = body
    return changes


def _validate_syntax(path: str, content: str) -> None:
    suffix = Path(path).suffix.lower()
    try:
        if suffix == ".py":
            ast.parse(content, filename=path)
        elif suffix in {".yaml", ".yml"}:
            yaml.safe_load(content)
        elif suffix == ".json":
            json.loads(content)
        elif suffix == ".toml":
            tomllib.loads(content)
    except Exception as exc:
        raise AuthorizedUpgradeError(f"候选 {path} 语法无效：{type(exc).__name__}: {exc}") from exc


def _changed_line_count(before: str, after: str) -> int:
    return sum(
        1
        for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="")
        if (line.startswith("+") or line.startswith("-")) and not line.startswith(("+++", "---"))
    )


def _prepare_changes(
    session: dict[str, Any],
    repo: Path,
    baseline_manifest: dict[str, str],
    changes: dict[str, str],
) -> list[dict[str, Any]]:
    plan = session["plan"]
    allowed_paths = plan["allowed_paths"]
    if not changes:
        raise AuthorizedUpgradeError("模型没有返回任何带 # FILE 标记的完整文件")
    if len(changes) > int(plan["max_files"]):
        raise AuthorizedUpgradeError(f"候选文件数 {len(changes)} 超过主人批准上限 {plan['max_files']}")

    records: list[dict[str, Any]] = []
    total_changed_lines = 0
    new_test = False
    for raw_path, content in changes.items():
        path = validate_repo_path(raw_path, allowed_paths)
        if len(content) > MAX_FILE_CHARS:
            raise AuthorizedUpgradeError(f"候选 {path} 超过 {MAX_FILE_CHARS} 字符上限")
        if path.startswith("app/tests/"):
            if path in baseline_manifest:
                raise AuthorizedUpgradeError(f"既有测试受保护，只能新增测试：{path}")
            if not Path(path).name.startswith("test_") or not path.endswith(".py"):
                raise AuthorizedUpgradeError("新增测试必须位于 app/tests/test_*.py")
            new_test = True
        _validate_syntax(path, content)
        scan_redlines(path, content)
        target = (repo / path).resolve()
        if not target.is_relative_to(repo.resolve()):
            raise AuthorizedUpgradeError(f"候选路径逃逸：{path}")
        before = target.read_text(encoding="utf-8") if target.is_file() else ""
        changed_lines = _changed_line_count(before, content)
        total_changed_lines += changed_lines
        records.append(
            {
                "path": path,
                "before_sha256": baseline_manifest.get(path, ""),
                "after_sha256": _sha256_bytes(content.encode("utf-8")),
                "changed_lines": changed_lines,
                "created": path not in baseline_manifest,
            }
        )

    if any(not item["path"].startswith(("docs/", "README.md")) for item in records) and not new_test:
        raise AuthorizedUpgradeError("生产代码或控制面升级必须新增至少一个 app/tests/test_*.py 回归测试")
    if total_changed_lines > int(plan["max_changed_lines"]):
        raise AuthorizedUpgradeError(
            f"候选变更行数 {total_changed_lines} 超过主人批准上限 {plan['max_changed_lines']}"
        )

    for path, content in changes.items():
        normalized = validate_repo_path(path, allowed_paths)
        target = repo / normalized
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return records


def _build_prompt(session: dict[str, Any], context: dict[str, str]) -> list[dict[str, str]]:
    sections = [f"### FILE: {path}\n```text\n{body}\n```" for path, body in context.items()]
    source = "\n\n".join(sections) or "（批准范围内没有已存在文件，可新增文件）"
    plan = session["plan"]
    prompt = f"""你是 Agenelf 的主人授权升级执行器。你可以修改主人批准范围内的代码和控制面文件，但不能扩大范围或触碰永久红线。

【升级目标】
{session['goal']}

【主人批准范围】
{json.dumps({'scopes': plan['scopes'], 'allowed_paths': plan['allowed_paths'], 'max_files': plan['max_files'], 'max_changed_lines': plan['max_changed_lines']}, ensure_ascii=False, indent=2)}

【当前文件】
{source}

【硬性输出契约】
1. 只输出需要创建或替换的完整文件，每个文件放在代码块中。
2. 每个代码块第一行必须是 # FILE: <仓库相对路径>。
3. 不得输出删除操作，不得修改批准范围之外的路径。
4. 不得修改任何既有 app/tests 文件；生产或控制面变更必须新增 app/tests/test_*.py。
5. 不得读取或写入 .env、local/、data/、secrets、审计记录、授权决定或 Git 元数据。
6. 禁止自我批准、伪造主人决定、削弱测试/门禁/审计、挂载 Docker Socket、执行模型生成任意 Shell、直接 push/merge main。
7. 保持改动最小并兼容现有接口；除代码块外不要输出解释。
"""
    return [
        {"role": "system", "content": "你是严谨的软件维护执行器，必须严格遵守主人批准的路径和永久安全红线。"},
        {"role": "user", "content": prompt},
    ]


def _run_tests(root: Path, repo: Path, baseline_manifest_path: Path, session_id: str) -> dict[str, Any]:
    runner = root / "scripts" / "run_authorized_upgrade_tests.py"
    if not runner.is_file():
        raise AuthorizedUpgradeError(f"可信升级测试 Runner 不存在：{runner}")
    try:
        process = subprocess.run(
            [
                sys.executable,
                str(runner),
                "--candidate-repo",
                str(repo),
                "--baseline-manifest",
                str(baseline_manifest_path),
                "--timeout",
                "600",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
        )
    except subprocess.TimeoutExpired as exc:
        raise AuthorizedUpgradeError("授权升级测试超时") from exc
    except OSError as exc:
        raise AuthorizedUpgradeError(f"无法启动授权升级测试：{exc}") from exc
    output = "\n".join(part for part in (process.stdout, process.stderr) if part)[-60_000:]
    try:
        report = json.loads(process.stdout or "{}")
    except json.JSONDecodeError:
        report = {"status": "invalid_test_report", "output": output}
    if not isinstance(report, dict):
        report = {"status": "invalid_test_report", "output": output}
    report["exit_code"] = process.returncode
    report["session_id"] = session_id
    report.setdefault("output", output)
    if process.returncode != 0:
        raise AuthorizedUpgradeError(
            "授权升级候选未通过可信测试：" + redact_sensitive_text(output[-5000:])
        )
    return report


def _request_candidate_approval(
    session: dict[str, Any], binding: dict[str, Any] | None = None
) -> str:
    # ``binding`` is accepted so the recovery adapter can reissue an approval for
    # the exact same candidate digest; the issued id is stored on the session.
    binding = dict(binding if isinstance(binding, dict) else session["candidate_binding"])
    ok, request_id = permissions.request_auth(
        "authorized_self_upgrade",
        "approve_tested_candidate",
        f"批准测试通过的精确升级候选：{session['id']}",
        reason="批准后仅由隔离 self-upgrade-runner 应用绑定摘要中的文件",
        ttl_seconds=900,
        binding=binding,
        operation="owner_authorized_self_upgrade_candidate",
        capability="agent.authorized_self_upgrade",
        channel="cli",
    )
    if not ok:
        raise AuthorizedUpgradeError(request_id)
    session["candidate_auth_id"] = request_id
    return request_id


def _advance_candidate(agent: Any, session: dict[str, Any], root: Path) -> dict[str, Any]:
    intent_id = str(session["intent_auth_id"])
    intent_binding = {key: value for key, value in session["plan"].items() if key != "fingerprint"}
    state = permissions.check_auth(intent_id, expected_binding=intent_binding)
    if state == permissions.STATUS_DENIED:
        session["status"] = "denied"
        _event(session, "intent", "主人拒绝升级意图")
        save_session(session, root)
        return session
    if state in {permissions.STATUS_PENDING, permissions.STATUS_NOT_FOUND}:
        return session
    if state == permissions.STATUS_EXPIRED:
        session["status"] = "expired"
        _event(session, "intent", "升级意图授权已过期")
        save_session(session, root)
        return session
    if state == permissions.STATUS_USED and not session.get("intent_consumed"):
        session["status"] = "failed"
        session["error"] = "升级意图授权已被其他流程核销"
        save_session(session, root)
        return session
    if state == permissions.STATUS_BINDING_MISMATCH:
        session["status"] = "failed"
        session["error"] = "升级意图授权绑定不匹配"
        save_session(session, root)
        return session
    if not session.get("intent_consumed"):
        if not permissions.consume_auth(intent_id, expected_binding=intent_binding):
            session["status"] = "failed"
            session["error"] = "升级意图授权核销失败"
            save_session(session, root)
            return session
        session["intent_consumed"] = True
        session["intent_consumed_at"] = now_iso()
        _event(session, "intent", f"主人意图授权 {intent_id} 已核销并进入候选阶段")
        save_session(session, root)

    source_app = root / "app-fork"
    if not source_app.is_dir():
        source_app = root / "app"
    marker = stage_workspace(root, source_app)
    repo = candidate_repo(root)
    baseline_manifest = _tree_manifest(repo)
    baseline_path = _write_manifest_file(session["id"], "baseline-manifest.json", baseline_manifest, root)
    session["workspace"] = marker
    session["baseline_manifest_path"] = str(baseline_path)
    session["status"] = "generating_candidate"
    save_session(session, root)

    context = _candidate_context(repo, session["plan"]["allowed_paths"], session["goal"])
    response = agent.llm.chat(_build_prompt(session, context), tools=None)
    if not isinstance(response, dict):
        raise AuthorizedUpgradeError("模型返回的升级候选不是对象")
    content = str(response.get("content") or "")
    changes = _parse_file_blocks(content)
    records = _prepare_changes(session, repo, baseline_manifest, changes)
    candidate_manifest = _tree_manifest(repo)
    candidate_manifest_path = _write_manifest_file(
        session["id"], "candidate-manifest.json", candidate_manifest, root
    )
    test_report = _run_tests(root, repo, baseline_path, session["id"])
    test_report_path = _write_manifest_file(session["id"], "test-report.json", test_report, root)
    changed_files = [item["path"] for item in records]
    candidate_binding = {
        "schema_version": SCHEMA_VERSION,
        "kind": "owner_authorized_self_upgrade_candidate",
        "session_id": session["id"],
        "intent_auth_id": intent_id,
        "goal_sha256": session["plan"]["goal_sha256"],
        "scopes": session["plan"]["scopes"],
        "allowed_paths": session["plan"]["allowed_paths"],
        "changed_files": records,
        "candidate_tree_sha256": _json_digest(candidate_manifest),
        "test_report_sha256": file_sha256(test_report_path),
        "baseline_manifest_sha256": file_sha256(baseline_path),
    }
    session.update(
        {
            "status": "awaiting_candidate_approval",
            "changed_files": changed_files,
            "changed_file_records": records,
            "candidate_manifest_path": str(candidate_manifest_path),
            "test_report_path": str(test_report_path),
            "candidate_binding": candidate_binding,
            "candidate_digest": candidate_binding["candidate_tree_sha256"],
        }
    )
    candidate_auth_id = _request_candidate_approval(session)
    session["candidate_auth_id"] = candidate_auth_id
    _event(
        session,
        "candidate",
        f"候选测试通过；已创建精确候选授权 {candidate_auth_id}，文件：{', '.join(changed_files)}",
    )
    save_session(session, root)
    return session


# --- Adapter surface used by core.authorized_upgrade_recovery -----------------
#
# The recovery skill installs bounded-retry wrappers on this module and expects
# the small helper interface below. Keep these names aligned with
# ``authorized_upgrade_recovery.install``; they are thin projections of the real
# internals above, so the wrapper never re-implements state machine details.

_RECOVERY_AUTH_STATES = {
    permissions.STATUS_APPROVED: "approved",
    # A consumed authorization is still the owner's approval; the precise
    # "consumed by whom" checks live in _advance_candidate/_submit_apply.
    permissions.STATUS_USED: "approved",
    permissions.STATUS_PENDING: "pending",
    permissions.STATUS_DENIED: "denied",
    permissions.STATUS_EXPIRED: "invalid",
    permissions.STATUS_NOT_FOUND: "invalid",
    permissions.STATUS_BINDING_MISMATCH: "invalid",
}


def _intent_auth_state(session: dict[str, Any]) -> str:
    """Coarse recovery-facing state of the exact intent authorization."""

    plan = session.get("plan", {}) if isinstance(session.get("plan"), dict) else {}
    binding = {key: value for key, value in plan.items() if key != "fingerprint"}
    state = permissions.check_auth(
        str(session.get("intent_auth_id") or ""), expected_binding=binding
    )
    return _RECOVERY_AUTH_STATES.get(state, "invalid")


def _candidate_auth_state(session: dict[str, Any]) -> str:
    """Coarse recovery-facing state of the exact candidate authorization."""

    binding = session.get("candidate_binding")
    state = permissions.check_auth(
        str(session.get("candidate_auth_id") or ""),
        expected_binding=binding if isinstance(binding, dict) else None,
    )
    return _RECOVERY_AUTH_STATES.get(state, "invalid")


def _request_intent_approval(session: dict[str, Any]) -> str:
    """Reissue the exact intent authorization (same plan binding) and store it."""

    request_id = _request_intent(session["plan"])
    session["intent_auth_id"] = request_id
    return request_id


def _generate_candidate(agent: Any, session: dict[str, Any]) -> dict[str, Any]:
    """Run the real candidate stage for the recovery wrapper's retry loop."""

    return _advance_candidate(agent, session, runtime_root())


def _request_payload(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "session_id": session["id"],
        "intent_auth_id": session["intent_auth_id"],
        "candidate_auth_id": session["candidate_auth_id"],
        "candidate_binding": session["candidate_binding"],
        "candidate_digest": session["candidate_digest"],
        "changed_files": session["changed_file_records"],
        "candidate_repo": str(candidate_repo(runtime_root())),
    }


def _submit_apply(session: dict[str, Any], root: Path) -> dict[str, Any]:
    candidate_id = str(session["candidate_auth_id"])
    binding = session["candidate_binding"]
    state = permissions.check_auth(candidate_id, expected_binding=binding)
    if state == permissions.STATUS_DENIED:
        session["status"] = "denied"
        _event(session, "candidate", "主人拒绝精确候选")
        save_session(session, root)
        return session
    if state in {permissions.STATUS_PENDING, permissions.STATUS_NOT_FOUND}:
        return session
    if state == permissions.STATUS_EXPIRED:
        session["status"] = "expired"
        _event(session, "candidate", "精确候选授权已过期")
        save_session(session, root)
        return session
    if state == permissions.STATUS_BINDING_MISMATCH:
        session["status"] = "failed"
        session["error"] = "精确候选授权绑定不匹配"
        save_session(session, root)
        return session
    if state == permissions.STATUS_USED and not session.get("apply_request_id"):
        session["status"] = "failed"
        session["error"] = "精确候选授权已被其他流程核销"
        save_session(session, root)
        return session

    if not session.get("apply_request_id"):
        request_id = REQUEST_PREFIX + uuid.uuid4().hex[:16]
        payload = _request_payload(session)
        request = {
            "id": request_id,
            "created_at": now_iso(),
            **payload,
        }
        request["fingerprint"] = _json_digest(payload)
        _atomic_json(data_paths(root)["requests"] / f"{request_id}.json", request, exclusive=True)
        session["apply_request_id"] = request_id
        session["status"] = "apply_queued"
        _event(session, "apply", f"已提交隔离应用请求 {request_id}")
        save_session(session, root)
    return session


def _reload_after_apply(agent: Any, session: dict[str, Any], result: dict[str, Any], root: Path) -> None:
    reloaded: list[str] = []
    reload_errors: list[str] = []
    for path in result.get("changed_files") or []:
        text = str(path)
        match = re.fullmatch(r"app/skills/([A-Za-z0-9_]+)\.py", text)
        if not match:
            continue
        name = match.group(1)
        if agent.registry.reload(name):
            agent.configure_skill_runtimes(name)
            reloaded.append(name)
        else:
            reload_errors.append(name)
    if reloaded:
        agent._refresh_system_prompt()
    session["hot_reloaded_skills"] = reloaded
    session["hot_reload_errors"] = reload_errors
    restart_required = bool(result.get("restart_required")) or bool(reload_errors)
    session["restart_required"] = restart_required
    if restart_required:
        try:
            from skills import task_continuation

            checkpoint = task_continuation.checkpoint(
                task_summary=session["goal"],
                resume_prompt=(
                    f"主人授权升级 {session['id']} 已应用。重新加载最新代码后，"
                    "读取 self-upgrade 结果和原任务证据，继续完成最初目标。"
                ),
                reason="owner_authorized_self_upgrade_restart",
                expires_minutes=1440,
                max_attempts=3,
            )
            session["continuation_id"] = checkpoint.get("id")
        except Exception as exc:
            session["continuation_error"] = redact_sensitive_text(f"{type(exc).__name__}: {exc}")[:1000]
    session["status"] = "restart_required" if restart_required else "succeeded"
    session["finished_at"] = now_iso()
    save_session(session, root)


def _poll_apply(agent: Any, session: dict[str, Any], root: Path, wait_seconds: float = 2.0) -> dict[str, Any]:
    request_id = str(session.get("apply_request_id") or "")
    if not request_id:
        return session
    path = data_paths(root)["results"] / f"{request_id}.json"
    deadline = time.monotonic() + max(0.0, min(float(wait_seconds), 30.0))
    while True:
        result = _read_json(path)
        if result is not None:
            session["apply_result"] = result
            if result.get("status") == "succeeded":
                _event(session, "apply", f"隔离应用成功：{request_id}")
                _reload_after_apply(agent, session, result, root)
            else:
                session["status"] = "failed"
                session["error"] = str(result.get("error") or result.get("reason") or "self-upgrade-runner failed")
                _event(session, "apply", session["error"])
                save_session(session, root)
            return session
        if time.monotonic() >= deadline:
            return session
        time.sleep(0.1)


def advance_session(agent: Any, session_id: str, *, wait_seconds: float = 2.0) -> dict[str, Any]:
    root = runtime_root()
    session = load_session(session_id, root)
    status = str(session.get("status", ""))
    try:
        if status == "awaiting_intent_approval":
            session = _advance_candidate(agent, session, root)
            status = str(session.get("status", ""))
        if status == "awaiting_candidate_approval":
            session = _submit_apply(session, root)
            status = str(session.get("status", ""))
        if status == "apply_queued":
            session = _poll_apply(agent, session, root, wait_seconds=wait_seconds)
    except Exception as exc:
        session["status"] = "failed"
        session["error"] = redact_sensitive_text(f"{type(exc).__name__}: {exc}")[:5000]
        _event(session, "failure", session["error"])
        save_session(session, root)
    return session


def route_goal(
    agent: Any,
    goal: object,
    scopes: Iterable[str] | None = None,
    *,
    max_files: int = DEFAULT_MAX_FILES,
    max_changed_lines: int = DEFAULT_MAX_CHANGED_LINES,
    wait_seconds: float = 2.0,
) -> dict[str, Any]:
    session = create_or_get_session(
        goal,
        scopes,
        max_files=max_files,
        max_changed_lines=max_changed_lines,
    )
    return advance_session(agent, str(session["id"]), wait_seconds=wait_seconds)


def public_status(session: dict[str, Any]) -> dict[str, Any]:
    plan = session.get("plan", {}) if isinstance(session.get("plan"), dict) else {}
    value = {
        "id": session.get("id"),
        "status": session.get("status"),
        "goal": session.get("goal"),
        "scopes": plan.get("scopes", []),
        "allowed_paths": plan.get("allowed_paths", []),
        "intent_auth_id": session.get("intent_auth_id"),
        "candidate_auth_id": session.get("candidate_auth_id"),
        "apply_request_id": session.get("apply_request_id"),
        "changed_files": session.get("changed_files", []),
        "hot_reloaded_skills": session.get("hot_reloaded_skills", []),
        "restart_required": bool(session.get("restart_required")),
        "continuation_id": session.get("continuation_id"),
        "error": session.get("error", ""),
        "updated_at": session.get("updated_at"),
    }
    status = str(session.get("status", ""))
    if status == "awaiting_intent_approval":
        value["next_action"] = f"在 Agenelf CLI 输入 /approve {session.get('intent_auth_id')}"
    elif status == "awaiting_candidate_approval":
        value["next_action"] = f"检查精确文件清单后输入 /approve {session.get('candidate_auth_id')}"
    elif status == "apply_queued":
        value["next_action"] = "self-upgrade-runner 正在验证并应用候选；再次查询状态"
    elif status == "restart_required":
        value["next_action"] = (
            "代码已应用且已保存续跑检查点；核心/Runner/Compose 变更需要重新创建相关容器后继续"
        )
    elif status == "succeeded":
        value["next_action"] = "已应用并热加载可重载技能；继续原始任务"
    return value


def latest_public_status(root: str | Path | None = None) -> dict[str, Any]:
    sessions = list_sessions(root, limit=1)
    return public_status(sessions[0]) if sessions else {"exists": False, "status": "none"}
