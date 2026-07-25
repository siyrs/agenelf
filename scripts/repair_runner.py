#!/usr/bin/env python3
"""Deterministic, network-isolated runner for owner-configured code repairs.

The runner clones a read-only local Git repository into ``repair-space``, validates
and applies a fingerprint-bound unified diff, executes only owner-configured argv
commands without a shell, and writes sanitized evidence.  It never commits, pushes,
merges, reads Agenelf secrets, or modifies the source repository.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

APP_FORK = Path(__file__).resolve().parents[1] / "app-fork"
if APP_FORK.is_dir() and str(APP_FORK) not in sys.path:
    sys.path.insert(0, str(APP_FORK))
APP_SOURCE = Path(__file__).resolve().parents[1] / "app"
if not APP_FORK.is_dir() and APP_SOURCE.is_dir() and str(APP_SOURCE) not in sys.path:
    sys.path.insert(0, str(APP_SOURCE))

from core import code_repair  # noqa: E402
from core.privacy import redact_sensitive_text  # noqa: E402

_ALIAS_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_SAFE_PATH_RE = re.compile(r"[A-Za-z0-9_./+@=-]+")
_ALLOWED_EXECUTABLES = {
    "python",
    "python3",
    "pytest",
    "mvn",
    "./mvnw",
    "gradle",
    "./gradlew",
    "npm",
    "pnpm",
    "yarn",
    "go",
    "cargo",
    "dotnet",
    "bash",
    "sh",
}
_GLOBAL_PROTECTED = (
    ".git/",
    ".github/workflows/",
    "local/",
    "secrets/",
    ".env",
    ".ops-runner.env",
    "policy/",
)
_MAX_OUTPUT_CHARS = 16_000
_MAX_COMMANDS = 8


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _safe_output(value: object, limit: int = _MAX_OUTPUT_CHARS) -> str:
    text = redact_sensitive_text(value)
    return text if len(text) <= limit else "…" + text[-limit:]


def _safe_relative(value: object, *, label: str) -> Path:
    text = str(value or "").strip().replace("\\", "/")
    path = Path(text)
    if not text or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{label} 必须是安全相对路径")
    if not _SAFE_PATH_RE.fullmatch(text):
        raise ValueError(f"{label} 含不支持字符")
    return path


def _under(root: Path, relative: Path, *, label: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} 逃逸出允许根目录") from exc
    return candidate


def _changed_paths(patch: str) -> list[str]:
    if "GIT binary patch" in patch or "Binary files " in patch:
        raise ValueError("暂不支持二进制补丁")
    values: list[str] = []
    for raw in patch.splitlines():
        if not raw.startswith("diff --git "):
            continue
        match = re.fullmatch(r"diff --git a/([^\s]+) b/([^\s]+)", raw)
        if match is None:
            raise ValueError("补丁路径必须是不含空格的标准 git 路径")
        left, right = match.groups()
        if left != right:
            raise ValueError("当前版本不支持重命名补丁")
        path = _safe_relative(right, label="补丁路径").as_posix()
        if path not in values:
            values.append(path)
    if not values:
        raise ValueError("补丁未包含任何标准 diff --git 文件")
    return values


def _path_is_protected(path: str, configured: list[str]) -> bool:
    normalized = path.lstrip("./")
    prefixes = list(_GLOBAL_PROTECTED) + configured
    for raw in prefixes:
        prefix = str(raw or "").strip().replace("\\", "/").lstrip("./")
        if not prefix:
            continue
        if prefix.endswith("/") and normalized.startswith(prefix):
            return True
        if normalized == prefix or normalized.startswith(prefix.rstrip("/") + "/"):
            return True
    return False


def _minimal_env(home: Path) -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True)
    tmp = home / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(home),
        "TMPDIR": str(tmp),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "CI": "1",
        "NO_PROXY": "*",
        "no_proxy": "*",
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "ALL_PROXY": "",
    }


class RepairRunner:
    def __init__(
        self,
        *,
        root: str | Path | None = None,
        config_file: str | Path | None = None,
        source_root: str | Path | None = None,
        repair_root: str | Path | None = None,
    ):
        self.root = (
            Path(root).resolve()
            if root is not None
            else Path(os.environ.get("AGENELF_ROOT", Path(__file__).resolve().parents[1])).resolve()
        )
        self.paths = code_repair.queue_paths(self.root)
        self.config_file = Path(
            config_file
            or os.environ.get("AGENELF_REPOSITORIES_FILE", "")
            or self.root / "local" / "repositories.yaml"
        ).resolve()
        self.source_root = Path(
            source_root
            or os.environ.get("AGENELF_CODE_WORKSPACES", "")
            or self.root / "code-workspaces"
        ).resolve()
        self.repair_root = Path(
            repair_root
            or os.environ.get("AGENELF_REPAIR_SPACE", "")
            or self.root / "repair-space"
        ).resolve()
        self.config = code_repair.load_repair_config(self.config_file)
        self.repositories, self.test_profiles = self._validate_config(self.config)

    @staticmethod
    def _validate_command(command: object, profile: str) -> list[str]:
        if not isinstance(command, list) or not command:
            raise ValueError(f"测试配置 {profile} 的 command 必须是非空 argv 数组")
        argv = [str(item) for item in command]
        if len(argv) > 64 or any(not item or len(item) > 2000 for item in argv):
            raise ValueError(f"测试配置 {profile} 的 argv 非法或过长")
        executable = argv[0]
        if executable not in _ALLOWED_EXECUTABLES:
            raise ValueError(f"测试配置 {profile} 不允许执行：{executable}")
        if executable in {"bash", "sh"} and any(item == "-c" for item in argv[1:]):
            raise ValueError(f"测试配置 {profile} 禁止 shell -c")
        if executable in {"python", "python3"} and any(item == "-c" for item in argv[1:]):
            raise ValueError(f"测试配置 {profile} 禁止 python -c")
        return argv

    def _validate_config(
        self, config: dict[str, Any]
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        if config.get("schema_version") != 1:
            raise ValueError("repositories.yaml schema_version 必须为 1")
        raw_profiles = config.get("test_profiles", {})
        profiles: dict[str, dict[str, Any]] = {}
        for raw_name, raw_profile in raw_profiles.items():
            name = str(raw_name)
            if not _ALIAS_RE.fullmatch(name) or not isinstance(raw_profile, dict):
                raise ValueError(f"非法测试配置：{name!r}")
            raw_commands = raw_profile.get("commands", [])
            if not isinstance(raw_commands, list) or not raw_commands or len(raw_commands) > _MAX_COMMANDS:
                raise ValueError(f"测试配置 {name} commands 必须有 1-{_MAX_COMMANDS} 项")
            commands = [self._validate_command(item, name) for item in raw_commands]
            timeout = max(1, min(int(raw_profile.get("timeout_seconds", 900)), 1800))
            profiles[name] = {"commands": commands, "timeout_seconds": timeout}

        raw_repositories = config.get("repositories", {})
        repositories: dict[str, dict[str, Any]] = {}
        for raw_alias, raw_profile in raw_repositories.items():
            alias = str(raw_alias)
            if not _ALIAS_RE.fullmatch(alias) or not isinstance(raw_profile, dict):
                raise ValueError(f"非法仓库配置：{alias!r}")
            source_dir = _safe_relative(raw_profile.get("source_dir", alias), label="source_dir")
            allowed = raw_profile.get("allowed_test_profiles", [])
            if not isinstance(allowed, list) or not allowed:
                raise ValueError(f"仓库 {alias} 必须配置 allowed_test_profiles")
            allowed_names = [str(item) for item in allowed]
            if any(item not in profiles for item in allowed_names):
                raise ValueError(f"仓库 {alias} 引用了未知测试配置")
            default_profile = str(raw_profile.get("default_test_profile", allowed_names[0]))
            if default_profile not in allowed_names:
                raise ValueError(f"仓库 {alias} 默认测试配置不在允许清单")
            protected = raw_profile.get("protected_paths", [])
            if not isinstance(protected, list):
                raise ValueError(f"仓库 {alias} protected_paths 必须是数组")
            repositories[alias] = {
                "source_dir": source_dir,
                "allowed_test_profiles": allowed_names,
                "default_test_profile": default_profile,
                "protected_paths": [str(item) for item in protected[:100]],
                "max_patch_files": max(1, min(int(raw_profile.get("max_patch_files", 20)), 100)),
                "max_patch_bytes": max(1024, min(int(raw_profile.get("max_patch_bytes", 262144)), 262144)),
            }
        return repositories, profiles

    def _run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                cwd=str(cwd),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                start_new_session=True,
            )
            return {
                "argv": argv,
                "exit_code": proc.returncode,
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
                "stdout_tail": _safe_output(proc.stdout),
                "stderr_tail": _safe_output(proc.stderr),
                "timed_out": False,
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "argv": argv,
                "exit_code": None,
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
                "stdout_tail": _safe_output(exc.stdout or ""),
                "stderr_tail": _safe_output(exc.stderr or ""),
                "timed_out": True,
            }
        except OSError as exc:
            return {
                "argv": argv,
                "exit_code": None,
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
                "stdout_tail": "",
                "stderr_tail": _safe_output(f"{type(exc).__name__}: {exc}"),
                "timed_out": False,
            }

    def _validate_request(self, request: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        if request.get("schema_version") != 1:
            raise ValueError("不支持的代码修复请求版本")
        patch = str(request.get("patch", ""))
        params = request.get("parameters", {})
        if not isinstance(params, dict):
            raise ValueError("parameters 必须是对象")
        digest = code_repair.patch_sha256(patch)
        if digest != params.get("patch_sha256"):
            raise ValueError("补丁摘要校验失败，请求可能被篡改")
        payload = code_repair.canonical_payload(
            str(request.get("target", "")),
            str(params.get("test_profile", "")),
            digest,
            len(patch.encode("utf-8")),
            expected_base=str(params.get("expected_base", "")),
        )
        if request.get("capability") != payload["capability"] or request.get("operation") != payload["operation"]:
            raise ValueError("请求能力或操作不受支持")
        if request.get("risk") != "read":
            raise ValueError("隔离代码修复请求风险必须为 read")
        if code_repair.payload_fingerprint(payload) != request.get("fingerprint"):
            raise ValueError("请求指纹校验失败，请求可能被篡改")
        alias = payload["target"]
        repository = self.repositories.get(alias)
        if repository is None:
            raise ValueError(f"未知仓库别名：{alias}")
        profile = payload["parameters"]["test_profile"]
        if profile not in repository["allowed_test_profiles"]:
            raise ValueError(f"仓库 {alias} 未允许测试配置 {profile}")
        if len(patch.encode("utf-8")) > repository["max_patch_bytes"]:
            raise ValueError("补丁超过仓库配置上限")
        paths = _changed_paths(patch)
        if len(paths) > repository["max_patch_files"]:
            raise ValueError("补丁文件数超过仓库配置上限")
        protected = repository["protected_paths"]
        blocked = [path for path in paths if _path_is_protected(path, protected)]
        if blocked:
            raise ValueError(f"补丁触碰受保护路径：{', '.join(blocked)}")
        return payload, {**repository, "changed_paths": paths}

    @staticmethod
    def _scan_symlinks(worktree: Path) -> None:
        root = worktree.resolve()
        for path in worktree.rglob("*"):
            if not path.is_symlink():
                continue
            target = path.resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"工作区包含逃逸符号链接：{path.relative_to(worktree)}") from exc

    def _execute(self, request: dict[str, Any], payload: dict[str, Any], repository: dict[str, Any]) -> dict[str, Any]:
        repair_id = str(request["id"])
        alias = str(payload["target"])
        source = _under(self.source_root, repository["source_dir"], label="source_dir")
        if source.is_symlink() or not source.is_dir() or not (source / ".git").exists():
            raise ValueError(f"仓库 {alias} 的只读源码目录不存在或不是 Git 仓库")

        run_dir = self.repair_root / repair_id
        if run_dir.exists():
            shutil.rmtree(run_dir)
        run_dir.mkdir(parents=True, mode=0o700)
        worktree = run_dir / "worktree"
        patch_path = run_dir / "candidate.patch"
        patch_path.write_text(str(request["patch"]), encoding="utf-8")

        clone = self._run(
            ["git", "clone", "--quiet", "--no-hardlinks", "--local", str(source), str(worktree)],
            cwd=run_dir,
            timeout=300,
            env=_minimal_env(run_dir / "home"),
        )
        commands: list[dict[str, Any]] = [{"phase": "clone", **clone}]
        if clone["exit_code"] != 0:
            return self._result(request, payload, "failed", commands, summary="无法复制只读源码仓库")

        self._scan_symlinks(worktree)
        base = self._run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            timeout=30,
            env=_minimal_env(run_dir / "home"),
        )
        commands.append({"phase": "base", **base})
        base_commit = base["stdout_tail"].strip().splitlines()[-1] if base["exit_code"] == 0 else ""
        expected = str(payload["parameters"].get("expected_base", ""))
        if expected and not base_commit.lower().startswith(expected.lower()):
            return self._result(
                request,
                payload,
                "blocked",
                commands,
                base_commit=base_commit,
                summary=f"源码基线 {base_commit[:12]} 与 expected_base {expected} 不一致",
            )

        check = self._run(
            ["git", "apply", "--check", "--whitespace=error-all", str(patch_path)],
            cwd=worktree,
            timeout=60,
            env=_minimal_env(run_dir / "home"),
        )
        commands.append({"phase": "patch_check", **check})
        if check["exit_code"] != 0:
            return self._result(request, payload, "failed", commands, base_commit=base_commit, summary="补丁无法应用")

        apply_result = self._run(
            ["git", "apply", "--whitespace=fix", str(patch_path)],
            cwd=worktree,
            timeout=60,
            env=_minimal_env(run_dir / "home"),
        )
        commands.append({"phase": "patch_apply", **apply_result})
        if apply_result["exit_code"] != 0:
            return self._result(request, payload, "failed", commands, base_commit=base_commit, summary="补丁应用失败")

        profile_name = str(payload["parameters"]["test_profile"])
        profile = self.test_profiles[profile_name]
        env = _minimal_env(run_dir / "home")
        tests_ok = True
        for argv in profile["commands"]:
            outcome = self._run(argv, cwd=worktree, timeout=profile["timeout_seconds"], env=env)
            commands.append({"phase": "test", **outcome})
            if outcome["exit_code"] != 0 or outcome["timed_out"]:
                tests_ok = False
                break

        diff = self._run(
            ["git", "diff", "--no-ext-diff", "--stat"],
            cwd=worktree,
            timeout=30,
            env=env,
        )
        commands.append({"phase": "diff_stat", **diff})
        status = "succeeded" if tests_ok else "failed"
        summary = "补丁已在隔离副本应用且全部测试通过" if tests_ok else "补丁已应用，但测试未通过"
        return self._result(
            request,
            payload,
            status,
            commands,
            base_commit=base_commit,
            changed_files=repository["changed_paths"],
            artifact_dir=f"repair-space/{repair_id}",
            summary=summary,
        )

    @staticmethod
    def _result(
        request: dict[str, Any],
        payload: dict[str, Any],
        status: str,
        commands: list[dict[str, Any]],
        *,
        base_commit: str = "",
        changed_files: list[str] | None = None,
        artifact_dir: str = "",
        summary: str = "",
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "id": request["id"],
            "capability": "code.repair",
            "operation": "apply_patch_and_test",
            "repository": payload["target"],
            "test_profile": payload["parameters"]["test_profile"],
            "status": status,
            "summary": redact_sensitive_text(summary),
            "base_commit": base_commit,
            "patch_sha256": payload["parameters"]["patch_sha256"],
            "changed_files": list(changed_files or []),
            "artifact_dir": artifact_dir,
            "commands": commands,
            "finished_at": now_iso(),
            "source_repository_modified": False,
            "committed": False,
            "pushed": False,
            "merged": False,
        }

    def process_request(self, request_path: Path) -> str:
        request = code_repair.read_json(request_path)
        if request is None:
            return "invalid"
        request_id = str(request.get("id", ""))
        result_path = self.paths["results"] / f"{request_id}.json"
        if result_path.exists():
            return "done"
        lock_path = self.paths["locks"] / f"{request_id}.lock"
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        except FileExistsError:
            return "locked"

        try:
            payload, repository = self._validate_request(request)
            result = self._execute(request, payload, repository)
        except Exception as exc:
            result = {
                "schema_version": 1,
                "id": request_id,
                "capability": "code.repair",
                "operation": "apply_patch_and_test",
                "status": "blocked",
                "summary": _safe_output(f"{type(exc).__name__}: {exc}", 2000),
                "commands": [],
                "finished_at": now_iso(),
                "source_repository_modified": False,
                "committed": False,
                "pushed": False,
                "merged": False,
            }
        _atomic_json(result_path, result)
        code_repair.audit(
            "repair_finished",
            f"{request_id} status={result.get('status')} repository={request.get('target', '')}",
            self.root,
        )
        try:
            lock_path.unlink()
        except OSError:
            pass
        return str(result.get("status", "failed"))

    def run_once(self) -> dict[str, int]:
        self.paths["requests"].mkdir(parents=True, exist_ok=True)
        counts: dict[str, int] = {}
        for path in sorted(self.paths["requests"].glob("repair-*.json")):
            state = self.process_request(path)
            counts[state] = counts.get(state, 0) + 1
        return counts

    def watch(self, interval: float = 1.0) -> None:
        code_repair.audit("repair_runner_started", f"repositories={','.join(sorted(self.repositories))}", self.root)
        while True:
            self.run_once()
            time.sleep(max(0.2, interval))


def main() -> int:
    parser = argparse.ArgumentParser(description="Agenelf isolated code repair runner")
    parser.add_argument("--once", action="store_true", help="process queue once and exit")
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    try:
        runner = RepairRunner()
        if args.once:
            print(json.dumps(runner.run_once(), ensure_ascii=False))
        else:
            runner.watch(args.interval)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"repair-runner 启动失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
