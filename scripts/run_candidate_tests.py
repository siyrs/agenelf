#!/usr/bin/env python3
"""Run controlled self-evolution tests without allowing baseline-test poisoning.

The candidate may add new tests, but every pre-existing baseline test and fixture must
remain byte-identical.  Trusted baseline tests and candidate-added tests run in
separate Python processes so a new test cannot monkey-patch the baseline suite before
it executes.
"""
from __future__ import annotations

import argparse
import compileall
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable

EXIT_BASELINE_TAMPERED = 10
EXIT_BASELINE_FAILED = 11
EXIT_NEW_TEST_FAILED = 12
EXIT_ENVIRONMENT_FAILED = 13
_MAX_OUTPUT = 40_000


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _trusted_files(app: Path) -> dict[str, str]:
    tests = app / "tests"
    if not tests.is_dir():
        raise RuntimeError(f"baseline tests directory missing: {tests}")
    result: dict[str, str] = {}
    for path in sorted(tests.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        result[path.relative_to(app).as_posix()] = _sha256(path)
    if not any(name.startswith("tests/test_") and name.endswith(".py") for name in result):
        raise RuntimeError("baseline contains no tests/test_*.py")
    return result


def _candidate_tests(app: Path) -> list[str]:
    tests = app / "tests"
    if not tests.is_dir():
        return []
    return [
        path.relative_to(app).as_posix()
        for path in sorted(tests.rglob("test_*.py"))
        if path.is_file() and "__pycache__" not in path.parts
    ]


def _environment(candidate: Path) -> dict[str, str]:
    repo = candidate.parent
    env = dict(os.environ)
    entries = [str(candidate), str(repo)]
    existing = env.get("PYTHONPATH", "")
    if existing:
        entries.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(entries)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run(candidate: Path, files: Iterable[str], *, timeout: int) -> tuple[bool, str]:
    selected = list(files)
    if not selected:
        return True, "（没有该类别测试）"
    command = [sys.executable, "-m", "unittest", "-v", *selected]
    try:
        process = subprocess.run(
            command,
            cwd=candidate,
            env=_environment(candidate),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        output = "\n".join(part for part in (exc.stdout, exc.stderr) if isinstance(part, str))
        return False, f"test timeout after {timeout}s\n{output[-_MAX_OUTPUT:]}"
    except OSError as exc:
        return False, f"cannot start unittest: {type(exc).__name__}: {exc}"
    output = "\n".join(part for part in (process.stdout, process.stderr) if part)
    if len(output) > _MAX_OUTPUT:
        output = output[-_MAX_OUTPUT:]
    return process.returncode == 0, output


def evaluate(baseline: Path, candidate: Path, *, phase: str, timeout: int) -> tuple[int, dict]:
    baseline = baseline.resolve()
    candidate = candidate.resolve()
    try:
        trusted = _trusted_files(baseline)
    except RuntimeError as exc:
        return EXIT_ENVIRONMENT_FAILED, {
            "status": "environment_failed",
            "phase": phase,
            "error": str(exc),
        }

    changed: list[str] = []
    missing: list[str] = []
    for relative, expected in trusted.items():
        path = candidate / relative
        if not path.is_file():
            missing.append(relative)
        elif _sha256(path) != expected:
            changed.append(relative)
    if missing or changed:
        return EXIT_BASELINE_TAMPERED, {
            "status": "baseline_tests_tampered",
            "phase": phase,
            "missing": missing,
            "changed": changed,
            "error": "既有测试受保护；候选只能新增测试，不能删除或修改基线测试",
        }

    compile_ok = compileall.compile_dir(
        str(candidate), quiet=2, force=True, legacy=False
    )
    if not compile_ok:
        return EXIT_ENVIRONMENT_FAILED, {
            "status": "compile_failed",
            "phase": phase,
            "error": "candidate Python compilation failed",
        }

    baseline_tests = sorted(
        name for name in trusted if name.startswith("tests/test_") and name.endswith(".py")
    )
    all_tests = _candidate_tests(candidate)
    new_tests = [name for name in all_tests if name not in set(baseline_tests)]

    baseline_ok, baseline_output = _run(candidate, baseline_tests, timeout=timeout)
    if not baseline_ok:
        return EXIT_BASELINE_FAILED, {
            "status": "baseline_failed",
            "phase": phase,
            "baseline_count": len(baseline_tests),
            "new_test_count": len(new_tests),
            "output": baseline_output,
            "error": "可信基线测试失败；禁止修改测试来掩盖该失败",
        }

    new_ok, new_output = _run(candidate, new_tests, timeout=timeout)
    if not new_ok:
        return EXIT_NEW_TEST_FAILED, {
            "status": "new_tests_failed",
            "phase": phase,
            "baseline_count": len(baseline_tests),
            "new_test_count": len(new_tests),
            "output": new_output,
            "error": "候选新增测试失败",
        }

    return 0, {
        "status": "passed",
        "phase": phase,
        "baseline_count": len(baseline_tests),
        "new_test_count": len(new_tests),
        "baseline_output": baseline_output[-5000:],
        "new_test_output": new_output[-5000:],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run trusted Agenelf candidate tests")
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--phase", choices=["baseline", "candidate"], default="candidate")
    parser.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args()
    code, result = evaluate(
        args.baseline,
        args.candidate,
        phase=args.phase,
        timeout=max(10, min(args.timeout, 1800)),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
