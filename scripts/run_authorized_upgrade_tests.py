#!/usr/bin/env python3
"""Deterministic verification for an owner-authorized repository candidate.

The candidate may change approved production/control-plane files, but every pre-existing
``app/tests`` and ``node/tests`` file must remain byte-identical. The verifier compiles
Python sources, parses YAML/JSON/TOML, checks shell syntax, runs the current trusted
governance validator against the candidate policy, and runs the complete Python suite.
A protected Python contract test separately runs the complete locked Node suite. This
verifier never imports a candidate control-plane helper into its own process and writes
bytecode only to a temporary cache, so the candidate can remain read-only.
"""
from __future__ import annotations

import argparse
import compileall
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

import yaml

MAX_OUTPUT = 80_000
_TEST_ROOTS = ("app/tests/", "node/tests/")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"JSON must be an object: {path}")
    return value


def verify_existing_tests(repo: Path, baseline: dict[str, str]) -> list[str]:
    checked: list[str] = []
    counts = {root: 0 for root in _TEST_ROOTS}
    for relative, expected in sorted(baseline.items()):
        root = next((prefix for prefix in _TEST_ROOTS if relative.startswith(prefix)), None)
        if root is None:
            continue
        path = repo / relative
        if not path.is_file():
            raise RuntimeError(f"existing test removed: {relative}")
        if path.is_symlink():
            raise RuntimeError(f"existing test replaced by symlink: {relative}")
        if sha256(path) != expected:
            raise RuntimeError(f"existing test modified: {relative}")
        checked.append(relative)
        counts[root] += 1
    if counts["app/tests/"] == 0:
        raise RuntimeError("baseline manifest contains no app/tests files")
    if (repo / "node" / "tests").is_dir() and counts["node/tests/"] == 0:
        raise RuntimeError("baseline manifest contains no node/tests files")
    return checked


def parse_structured_files(repo: Path) -> dict[str, int]:
    counts = {"yaml": 0, "json": 0, "toml": 0}
    roots = [repo / "policy", repo / ".github", repo]
    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        candidates = [root] if root.is_file() else list(root.rglob("*"))
        for path in candidates:
            if not path.is_file() or path in seen or "app-tmp" in path.parts:
                continue
            seen.add(path)
            suffix = path.suffix.lower()
            name = path.name
            try:
                if suffix in {".yaml", ".yml"} or name == "docker-compose.yml":
                    yaml.safe_load(path.read_text(encoding="utf-8"))
                    counts["yaml"] += 1
                elif suffix == ".json":
                    json.loads(path.read_text(encoding="utf-8"))
                    counts["json"] += 1
                elif suffix == ".toml":
                    tomllib.loads(path.read_text(encoding="utf-8"))
                    counts["toml"] += 1
            except Exception as exc:
                raise RuntimeError(
                    f"structured file invalid: {path.relative_to(repo)}: {exc}"
                ) from exc
    return counts


def run_command(
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    try:
        process = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        output = "\n".join(
            part for part in (exc.stdout, exc.stderr) if isinstance(part, str)
        )
        return {
            "ok": False,
            "exit_code": 124,
            "command": command,
            "output": output[-MAX_OUTPUT:],
        }
    except OSError as exc:
        return {
            "ok": False,
            "exit_code": 126,
            "command": command,
            "output": f"{type(exc).__name__}: {exc}",
        }
    output = "\n".join(part for part in (process.stdout, process.stderr) if part)
    return {
        "ok": process.returncode == 0,
        "exit_code": process.returncode,
        "command": command,
        "output": output[-MAX_OUTPUT:],
    }


def shell_checks(repo: Path, timeout: int) -> list[dict[str, Any]]:
    bash = shutil.which("bash")
    scripts = (
        sorted((repo / "scripts").glob("*.sh"))
        if (repo / "scripts").is_dir()
        else []
    )
    if not scripts:
        return []
    if not bash:
        raise RuntimeError("bash is required to validate candidate shell scripts")
    results = []
    for script in scripts:
        result = run_command(
            [bash, "-n", str(script)],
            cwd=repo,
            timeout=min(timeout, 60),
        )
        results.append(result)
        if not result["ok"]:
            raise RuntimeError(
                f"shell syntax failed: {script.name}: {result['output'][-2000:]}"
            )
    return results


def compile_candidate(app: Path, scripts_dir: Path) -> None:
    old_prefix = sys.pycache_prefix
    with tempfile.TemporaryDirectory(prefix="agenelf-upgrade-pycache-") as cache:
        try:
            sys.pycache_prefix = cache
            compile_ok = compileall.compile_dir(str(app), quiet=2, force=True)
            if scripts_dir.is_dir():
                compile_ok = (
                    compileall.compile_dir(str(scripts_dir), quiet=2, force=True)
                    and compile_ok
                )
        finally:
            sys.pycache_prefix = old_prefix
    if not compile_ok:
        raise RuntimeError("candidate Python compilation failed")


def trusted_governance_check(
    repo: Path,
    baseline: dict[str, str],
    timeout: int,
) -> dict[str, Any] | None:
    policy = repo / "policy" / "safety-constraints.v1.yaml"
    policy_was_in_baseline = "policy/safety-constraints.v1.yaml" in baseline
    if not policy_was_in_baseline and not policy.is_file():
        return None
    if not policy.is_file():
        raise RuntimeError("candidate removed policy/safety-constraints.v1.yaml")
    validator = Path(__file__).resolve().with_name("validate_governance.py")
    if not validator.is_file():
        raise RuntimeError(f"trusted governance validator missing: {validator}")
    result = run_command(
        [sys.executable, str(validator), str(policy)],
        cwd=repo,
        timeout=min(timeout, 180),
    )
    if not result["ok"]:
        raise RuntimeError(
            "trusted governance validation failed:\n" + result["output"][-8000:]
        )
    return {
        "exit_code": result["exit_code"],
        "output_tail": result["output"][-4000:],
        "validator": str(validator),
    }


def evaluate(
    candidate_repo: Path,
    baseline_manifest: Path,
    timeout: int,
) -> tuple[int, dict[str, Any]]:
    repo = candidate_repo.resolve()
    app = repo / "app"
    try:
        if not app.is_dir() or not (app / "tests").is_dir():
            raise RuntimeError(f"candidate repository is incomplete: {repo}")
        baseline = load_json(baseline_manifest)
        trusted_tests = verify_existing_tests(repo, baseline)
        structured = parse_structured_files(repo)
        scripts_dir = repo / "scripts"
        compile_candidate(app, scripts_dir)
        shell = shell_checks(repo, timeout)
        governance = trusted_governance_check(repo, baseline, timeout)

        env = dict(os.environ)
        entries = [str(app), str(repo)]
        if env.get("PYTHONPATH"):
            entries.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(entries)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        tests = run_command(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            cwd=app,
            timeout=timeout,
            env=env,
        )
        if not tests["ok"]:
            raise RuntimeError(
                "complete unittest suite failed:\n" + tests["output"][-8000:]
            )
        return 0, {
            "status": "passed",
            "candidate_repo": str(repo),
            "trusted_existing_tests": len(trusted_tests),
            "trusted_app_tests": sum(item.startswith("app/tests/") for item in trusted_tests),
            "trusted_node_tests": sum(item.startswith("node/tests/") for item in trusted_tests),
            "structured_files": structured,
            "shell_scripts": len(shell),
            "trusted_governance": governance,
            "unittest": {
                "exit_code": tests["exit_code"],
                "output_tail": tests["output"][-10_000:],
            },
        }
    except Exception as exc:
        return 1, {
            "status": "failed",
            "candidate_repo": str(repo),
            "error": f"{type(exc).__name__}: {exc}",
        }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify an owner-authorized Agenelf upgrade candidate"
    )
    parser.add_argument("--candidate-repo", required=True, type=Path)
    parser.add_argument("--baseline-manifest", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    code, report = evaluate(
        args.candidate_repo,
        args.baseline_manifest,
        max(30, min(int(args.timeout), 1800)),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
