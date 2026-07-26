"""Deterministic repository-shaped workspaces for controlled self-evolution.

The Agent may write only inside ``app-tmp``.  A real candidate nevertheless needs the
same repository fixtures as CI (``.github/``, policy, scripts, Compose topology, docs
and examples), otherwise repository-contract tests fail for environmental reasons and
the model is tempted to "repair" trusted tests.  This module stages a safe, explicitly
mounted repository snapshot and records hashes for every pre-existing test file.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any


class EvolutionWorkspaceError(RuntimeError):
    """Raised when a controlled candidate workspace cannot be prepared safely."""


_IGNORED_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
_IGNORED_SUFFIXES = {".pyc", ".pyo"}
_WORKSPACE_MARKER = ".agenelf-evolution-workspace.json"


def runtime_root() -> Path:
    configured = os.environ.get("AGENELF_ROOT", "").strip()
    return Path(configured).resolve() if configured else Path(__file__).resolve().parents[2]


def temporary_root(root: Path) -> Path:
    return root / "app-tmp"


def repository_source(root: Path) -> Path:
    """Return the selectively mounted, non-secret repository fixture directory."""

    return root / "repo-source"


def uses_repository_layout(root: Path) -> bool:
    """Real containers expose safe repository fixtures at ``repo-source``.

    Isolated legacy unit tests do not, so they retain the historic direct ``app-tmp``
    layout.  This compatibility path keeps old integrity tests meaningful while real
    deployments receive a repository-shaped candidate.
    """

    return repository_source(root).is_dir()


def candidate_repo(root: Path) -> Path:
    tmp = temporary_root(root)
    return tmp / "repo" if uses_repository_layout(root) else tmp


def candidate_app(root: Path) -> Path:
    repo = candidate_repo(root)
    return repo / "app" if uses_repository_layout(root) else repo


def _ignored(path: Path) -> bool:
    return any(part in _IGNORED_NAMES for part in path.parts) or path.suffix in _IGNORED_SUFFIXES


def clear_tree_contents(path: Path, retries: int = 5) -> None:
    """Clear a directory without deleting its root mount point.

    ``app-tmp`` is a bind mount in production.  Deleting the mount root is invalid and
    caused the previous ``File exists`` loop.  Contents are removed with verification
    and bounded retries instead.
    """

    import time

    path.mkdir(parents=True, exist_ok=True)
    for attempt in range(max(1, retries)):
        failures: list[str] = []
        for item in list(path.iterdir()):
            try:
                if item.is_symlink() or item.is_file():
                    item.unlink()
                elif item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink(missing_ok=True)
            except OSError as exc:
                failures.append(f"{item.name}: {exc}")
        remaining = list(path.iterdir())
        if not remaining:
            return
        time.sleep(0.15 * (attempt + 1))
    remaining = [item.name for item in path.iterdir()]
    raise EvolutionWorkspaceError(
        "app-tmp 内容无法清空；不会删除挂载点本身。"
        f"残留：{remaining[:10]}"
    )


def copy_tree(source: Path, destination: Path) -> int:
    """Copy regular files only, rejecting symlinks and transient bytecode."""

    if not source.is_dir():
        raise EvolutionWorkspaceError(f"源码目录不存在：{source}")
    count = 0
    destination.mkdir(parents=True, exist_ok=True)
    for current in sorted(source.rglob("*")):
        relative = current.relative_to(source)
        if _ignored(relative):
            continue
        if current.is_symlink():
            raise EvolutionWorkspaceError(f"候选基线含符号链接，拒绝复制：{current}")
        target = destination / relative
        if current.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not current.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(current, target)
        count += 1
    return count


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def baseline_test_manifest(source_app: Path) -> dict[str, str]:
    """Hash every trusted test file/fixture under the baseline ``tests`` tree."""

    tests = source_app / "tests"
    if not tests.is_dir():
        raise EvolutionWorkspaceError(f"基线缺少 tests 目录：{tests}")
    manifest: dict[str, str] = {}
    for path in sorted(tests.rglob("*")):
        if _ignored(path.relative_to(tests)) or not path.is_file():
            continue
        if path.is_symlink():
            raise EvolutionWorkspaceError(f"基线测试含符号链接：{path}")
        relative = path.relative_to(source_app).as_posix()
        manifest[relative] = file_sha256(path)
    if not any(name.startswith("tests/test_") and name.endswith(".py") for name in manifest):
        raise EvolutionWorkspaceError("基线 tests/ 中没有 test_*.py")
    return manifest


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def stage_workspace(root: Path, source_app: Path) -> dict[str, Any]:
    """Create a clean candidate and return an auditable staging manifest."""

    root = root.resolve()
    source_app = source_app.resolve()
    tmp = temporary_root(root)
    clear_tree_contents(tmp)

    layout = "repository" if uses_repository_layout(root) else "legacy-app-root"
    repo = candidate_repo(root)
    app = candidate_app(root)
    repo.mkdir(parents=True, exist_ok=True)

    fixture_count = 0
    if layout == "repository":
        safe_source = repository_source(root)
        # repo-source is assembled only from explicit non-secret read-only mounts.
        fixture_count += copy_tree(safe_source, repo)
        # scripts and policy are already selectively mounted at the runtime root.
        for name in ("scripts", "policy"):
            source = root / name
            if source.is_dir():
                destination = repo / name
                if destination.exists():
                    shutil.rmtree(destination)
                fixture_count += copy_tree(source, destination)

    app_count = copy_tree(source_app, app)
    tests = baseline_test_manifest(source_app)
    marker = {
        "schema_version": 1,
        "layout": layout,
        "source_app": str(source_app),
        "candidate_repo": str(repo),
        "candidate_app": str(app),
        "app_file_count": app_count,
        "fixture_file_count": fixture_count,
        "baseline_tests": tests,
    }
    _atomic_json(repo / _WORKSPACE_MARKER, marker)
    return marker


def load_workspace_marker(root: Path) -> dict[str, Any] | None:
    path = candidate_repo(root) / _WORKSPACE_MARKER
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def validate_relative_app_path(raw: str) -> str:
    value = str(raw or "").replace("\\", "/").strip()
    if not value or value.startswith("/"):
        raise EvolutionWorkspaceError("候选路径必须是 app 根目录下的相对路径")
    parts = [part for part in value.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise EvolutionWorkspaceError(f"候选路径逃逸：{raw!r}")
    normalized = "/".join(parts)
    if not normalized.startswith(("core/", "skills/", "tests/")):
        raise EvolutionWorkspaceError("候选只能修改 core/、skills/ 或新增 tests/test_*.py")
    if not normalized.endswith(".py"):
        raise EvolutionWorkspaceError("自主候选只允许 Python 文件")
    return normalized


def candidate_path(root: Path, relative: str) -> Path:
    normalized = validate_relative_app_path(relative)
    app = candidate_app(root).resolve()
    destination = (app / normalized).resolve()
    if not destination.is_relative_to(app):
        raise EvolutionWorkspaceError(f"候选路径逃逸：{relative!r}")
    return destination


def assert_trusted_tests_unchanged(candidate: Path, manifest: dict[str, str]) -> None:
    for relative, expected in manifest.items():
        path = candidate / relative
        if not path.is_file():
            raise EvolutionWorkspaceError(f"既有测试被删除：{relative}")
        if file_sha256(path) != expected:
            raise EvolutionWorkspaceError(
                f"既有测试受保护，不能通过修改测试绕过门禁：{relative}"
            )
