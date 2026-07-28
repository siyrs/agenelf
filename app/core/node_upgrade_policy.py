"""Node.js/TypeScript extensions for the owner-authorized upgrade engine.

The mature Python upgrade engine remains the single workflow and evidence source. This
module installs narrowly-scoped Node path, syntax, regression-test and redline rules on
that engine when the ``core`` package is imported. It never changes the two exact owner
approvals, immutable candidate binding, trusted Runner, rollback, or forbidden data
roots.
"""
from __future__ import annotations

import ast
import difflib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable

_INSTALLED = False
_NODE_SCOPES = {"node_runtime", "node_skills", "node_runners", "node_tests", "node_build", "contracts"}
_NODE_SUFFIXES = {".ts", ".mts", ".cts"}
_NODE_TEST_RE = re.compile(r"^node/tests/[A-Za-z0-9_.-]+\.test\.ts$")
_NODE_REDLINE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "Node 任意 Shell",
        re.compile(
            r"(?is)(?:from\s+['\"]node:child_process['\"]|require\(['\"](?:node:)?child_process['\"]\))"
            r".{0,1200}(?:\bexec(?:Sync)?\s*\(|\bspawn(?:Sync)?\s*\([^\n]{0,300}\bshell\s*:\s*true)",
        ),
    ),
    ("Node 动态代码执行", re.compile(r"(?i)\b(?:eval|Function)\s*\(|vm\.(?:runIn|compileFunction)")),
    ("关闭 TLS 校验", re.compile(r"NODE_TLS_REJECT_UNAUTHORIZED\s*[:=]\s*['\"]?0")),
)
_FORBIDDEN_LIFECYCLE_SCRIPTS = {
    "preinstall",
    "install",
    "postinstall",
    "prepublish",
    "prepublishOnly",
    "prepare",
}


def _changed_line_count(before: str, after: str) -> int:
    return sum(
        1
        for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="")
        if (line.startswith("+") or line.startswith("-")) and not line.startswith(("+++", "---"))
    )


def _validate_node_syntax(path: str, content: str) -> None:
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node.js 不存在，不能验证 TypeScript 升级候选")
    suffix = Path(path).suffix.lower() or ".ts"
    with tempfile.TemporaryDirectory(prefix="agenelf-node-upgrade-syntax-") as directory:
        candidate = Path(directory) / f"candidate{suffix}"
        candidate.write_text(content, encoding="utf-8")
        process = subprocess.run(
            [node, "--check", str(candidate)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    if process.returncode != 0:
        output = "\n".join(part for part in (process.stdout, process.stderr) if part)[-3000:]
        raise RuntimeError(output or "Node TypeScript syntax check failed")


def _node_scan_redlines(base: Any, path: str, content: str) -> None:
    for label, pattern in _NODE_REDLINE_PATTERNS:
        if pattern.search(content):
            raise base.AuthorizedUpgradeError(f"候选 {path} 命中永久安全红线：{label}")
    if path == "package.json":
        try:
            value = json.loads(content)
        except json.JSONDecodeError as exc:
            raise base.AuthorizedUpgradeError(f"候选 package.json 语法无效：{exc}") from exc
        scripts = value.get("scripts", {}) if isinstance(value, dict) else {}
        if not isinstance(scripts, dict):
            raise base.AuthorizedUpgradeError("候选 package.json scripts 必须是对象")
        forbidden = sorted(_FORBIDDEN_LIFECYCLE_SCRIPTS.intersection(map(str, scripts)))
        if forbidden:
            raise base.AuthorizedUpgradeError(
                "候选 package.json 禁止新增 npm 生命周期脚本：" + ", ".join(forbidden)
            )


def _prepare_changes(base: Any, session: dict[str, Any], repo: Path, baseline_manifest: dict[str, str], changes: dict[str, str]) -> list[dict[str, Any]]:
    plan = session["plan"]
    allowed_paths = plan["allowed_paths"]
    if not changes:
        raise base.AuthorizedUpgradeError("模型没有返回任何带 # FILE 标记的完整文件")
    if len(changes) > int(plan["max_files"]):
        raise base.AuthorizedUpgradeError(
            f"候选文件数 {len(changes)} 超过主人批准上限 {plan['max_files']}"
        )

    records: list[dict[str, Any]] = []
    total_changed_lines = 0
    new_regression_test = False
    for raw_path, content in changes.items():
        path = base.validate_repo_path(raw_path, allowed_paths)
        if len(content) > base.MAX_FILE_CHARS:
            raise base.AuthorizedUpgradeError(f"候选 {path} 超过 {base.MAX_FILE_CHARS} 字符上限")
        if path.startswith("app/tests/"):
            if path in baseline_manifest:
                raise base.AuthorizedUpgradeError(f"既有测试受保护，只能新增测试：{path}")
            if not Path(path).name.startswith("test_") or not path.endswith(".py"):
                raise base.AuthorizedUpgradeError("新增 Python 测试必须位于 app/tests/test_*.py")
            new_regression_test = True
        elif path.startswith("node/tests/"):
            if path in baseline_manifest:
                raise base.AuthorizedUpgradeError(f"既有 Node 测试受保护，只能新增测试：{path}")
            if not _NODE_TEST_RE.fullmatch(path):
                raise base.AuthorizedUpgradeError("新增 Node 测试必须位于 node/tests/*.test.ts")
            new_regression_test = True

        base._validate_syntax(path, content)
        base.scan_redlines(path, content)
        target = (repo / path).resolve()
        if not target.is_relative_to(repo.resolve()):
            raise base.AuthorizedUpgradeError(f"候选路径逃逸：{path}")
        before = target.read_text(encoding="utf-8") if target.is_file() else ""
        changed_lines = _changed_line_count(before, content)
        total_changed_lines += changed_lines
        records.append(
            {
                "path": path,
                "before_sha256": baseline_manifest.get(path, ""),
                "after_sha256": base._sha256_bytes(content.encode("utf-8")),
                "changed_lines": changed_lines,
                "created": path not in baseline_manifest,
            }
        )

    production_change = any(
        not item["path"].startswith(("docs/", "README.md")) for item in records
    )
    if production_change and not new_regression_test:
        raise base.AuthorizedUpgradeError(
            "生产代码或控制面升级必须新增 app/tests/test_*.py 或 node/tests/*.test.ts 回归测试"
        )
    if total_changed_lines > int(plan["max_changed_lines"]):
        raise base.AuthorizedUpgradeError(
            f"候选变更行数 {total_changed_lines} 超过主人批准上限 {plan['max_changed_lines']}"
        )

    for path, content in changes.items():
        normalized = base.validate_repo_path(path, allowed_paths)
        target = repo / normalized
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return records


def _build_prompt(base: Any, session: dict[str, Any], context: dict[str, str]) -> list[dict[str, str]]:
    sections = [f"### FILE: {path}\n```text\n{body}\n```" for path, body in context.items()]
    source = "\n\n".join(sections) or "（批准范围内没有已存在文件，可新增文件）"
    plan = session["plan"]
    prompt = f"""你是 Agenelf 的主人授权升级执行器。你可以修改主人批准范围内的 Python、Node.js/TypeScript 或控制面文件，但不能扩大范围或触碰永久红线。

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
4. 不得修改任何既有 app/tests 或 node/tests 文件；生产或控制面变更必须新增 app/tests/test_*.py 或 node/tests/*.test.ts。
5. 不得读取或写入 .env、local/、data/、secrets、审计记录、授权决定或 Git 元数据。
6. 禁止自我批准、伪造主人决定、削弱测试/门禁/审计、挂载 Docker Socket、任意 Shell/动态代码执行、直接 push/merge main。
7. package.json 禁止 preinstall/install/postinstall/prepare 等生命周期脚本；依赖安装保持 npm ci --ignore-scripts。
8. 保持改动最小并兼容现有接口；除代码块外不要输出解释。
"""
    return [
        {"role": "system", "content": "你是严谨的软件维护执行器，必须严格遵守主人批准的路径和永久安全红线。"},
        {"role": "user", "content": prompt},
    ]


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    from core import authorized_upgrade as base

    base._ALLOWED_SUFFIXES.update(_NODE_SUFFIXES)
    base._ALLOWED_BASENAMES.update(
        {
            "Dockerfile.node",
            "Dockerfile.control-plane",
            "docker-compose.python.yml",
            "docker-compose.override.yml",
            "docker-compose.node-approval.yml",
            "compose.yaml",
            "compose.override.yaml",
            "package.json",
            "package-lock.json",
            ".node-version",
        }
    )
    base._SCOPE_PATHS.update(
        {
            "node_runtime": ("node/packages/core/", "node/apps/api/", "node/apps/cli/"),
            "node_skills": ("node/packages/skills/",),
            "node_runners": ("node/apps/runner/", "node/apps/validation-runner/", "node/apps/approval-runner/", "node/apps/approval-key-init/"),
            "node_tests": ("node/tests/",),
            "node_build": (
                "package.json",
                "package-lock.json",
                ".node-version",
                "node/tsconfig.json",
                "Dockerfile.node",
                "Dockerfile.control-plane",
            ),
            "contracts": ("contracts/",),
            "compose": (
                "compose.yaml",
                "compose.override.yaml",
                "docker-compose.yml",
                "docker-compose.override.yml",
                "docker-compose.node-approval.yml",
                "docker-compose.python.yml",
                "Dockerfile",
                "Dockerfile.node",
                "Dockerfile.control-plane",
                ".env.example",
                ".ops-runner.env.example",
            ),
        }
    )
    base._SCOPE_PATTERNS = (
        ("node_runners", re.compile(r"(?i)Node(?:\.js)?\s*(?:validation|ops|approval|repair|upgrade)?\s*runner|TypeScript\s*runner|Node\s*执行器")),
        ("node_runtime", re.compile(r"(?i)Node(?:\.js)?\s*(?:Agent|API|CLI|runtime|core)|TypeScript\s*(?:Agent|API|CLI|runtime|core)|node/packages/core|node/apps/(?:api|cli)")),
        ("node_skills", re.compile(r"(?i)Node(?:\.js)?\s*skill|TypeScript\s*skill|node/packages/skills")),
        ("node_tests", re.compile(r"(?i)Node(?:\.js)?\s*test|TypeScript\s*test|node/tests|Vitest|Playwright")),
        ("node_build", re.compile(r"(?i)package(?:-lock)?\.json|Dockerfile\.node|node/tsconfig|Node\s*构建|npm\s*(?:ci|test)")),
        ("contracts", re.compile(r"(?i)contracts/|JSON\s*Schema|协议契约|event envelope|session ledger schema")),
        *base._SCOPE_PATTERNS,
    )

    original_classify = base.classify_scopes
    original_validate_syntax = base._validate_syntax
    original_scan_redlines = base.scan_redlines

    def classify_scopes(goal: object, hints: Iterable[str] | None = None) -> list[str]:
        scopes = set(original_classify(goal, hints))
        if scopes.intersection(_NODE_SCOPES - {"node_tests"}):
            scopes.add("node_tests")
            scopes.add("tests")
        return sorted(scopes)

    def validate_syntax(path: str, content: str) -> None:
        if Path(path).suffix.lower() in _NODE_SUFFIXES:
            try:
                _validate_node_syntax(path, content)
            except Exception as exc:
                raise base.AuthorizedUpgradeError(
                    f"候选 {path} TypeScript 语法无效：{type(exc).__name__}: {exc}"
                ) from exc
            return
        original_validate_syntax(path, content)

    def scan_redlines(path: str, content: str) -> None:
        original_scan_redlines(path, content)
        _node_scan_redlines(base, path, content)

    base.classify_scopes = classify_scopes
    base._validate_syntax = validate_syntax
    base.scan_redlines = scan_redlines
    base._prepare_changes = lambda session, repo, baseline, changes: _prepare_changes(base, session, repo, baseline, changes)
    base._build_prompt = lambda session, context: _build_prompt(base, session, context)
    base.NODE_UPGRADE_POLICY_VERSION = 1
    _INSTALLED = True
