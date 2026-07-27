#!/usr/bin/env python3
"""Deterministic application runner for owner-authorized Agenelf upgrades.

The runner has no network, Docker socket, SSH material, owner profile or Git metadata.
It reads a tested candidate from ``app-tmp`` and can write only to repository paths
explicitly mounted below ``/agenelf/upgrade-target``.  It revalidates both owner
approvals, candidate hashes, current target hashes, redlines and the complete test suite
before applying an exact file manifest.  Partial writes are rolled back from a local
backup.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("AGENELF_ROOT", "/agenelf")).resolve()
APP_DIR = ROOT / "app-fork"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from core import authorized_upgrade, permissions  # noqa: E402

TARGET_ROOT = Path(os.environ.get("AGENELF_UPGRADE_TARGET", ROOT / "upgrade-target")).resolve()
CANDIDATE_REPO = (ROOT / "app-tmp" / "repo").resolve()
MAX_OUTPUT = 60_000


class SelfUpgradeRunnerError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def atomic_json(path: Path, value: dict[str, Any], *, exclusive: bool = False) -> None:
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


def json_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_manifest(root: Path) -> dict[str, str]:
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


def audit(event: str, detail: object) -> None:
    path = ROOT / "logs" / "self-upgrade-runner.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"[{now_iso()}] [{event}] {str(detail)[:3000]}\n")
    except OSError:
        pass


def queue_paths() -> dict[str, Path]:
    data = ROOT / "data"
    return {
        "requests": data / "self-upgrade-requests",
        "results": data / "self-upgrade-results",
        "locks": data / "self-upgrade-locks",
        "backups": data / "self-upgrade-backups",
        "sessions": data / "authorized-upgrades",
    }


def canonical_request_payload(request: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": request.get("schema_version"),
        "session_id": request.get("session_id"),
        "intent_auth_id": request.get("intent_auth_id"),
        "candidate_auth_id": request.get("candidate_auth_id"),
        "candidate_binding": request.get("candidate_binding"),
        "candidate_digest": request.get("candidate_digest"),
        "changed_files": request.get("changed_files"),
        "candidate_repo": request.get("candidate_repo"),
    }


def target_path(relative: str) -> Path:
    normalized = str(relative).replace("\\", "/").lstrip("./")
    path = (TARGET_ROOT / normalized).resolve()
    if not path.is_relative_to(TARGET_ROOT):
        raise SelfUpgradeRunnerError(f"target path escapes upgrade root: {relative}")
    return path


def validate_request(request: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    request_id = str(request.get("id", ""))
    if not request_id.startswith("self-upgrade-") or len(request_id) > 80:
        raise SelfUpgradeRunnerError("invalid self-upgrade request id")
    payload = canonical_request_payload(request)
    if json_digest(payload) != str(request.get("fingerprint", "")):
        raise SelfUpgradeRunnerError("request fingerprint mismatch")
    if request.get("schema_version") != authorized_upgrade.SCHEMA_VERSION:
        raise SelfUpgradeRunnerError("unsupported self-upgrade request version")
    session_id = str(request.get("session_id", ""))
    session = authorized_upgrade.load_session(session_id, ROOT)
    if session.get("status") not in {"apply_queued", "awaiting_candidate_approval"}:
        raise SelfUpgradeRunnerError(f"session state cannot be applied: {session.get('status')}")
    if not session.get("intent_consumed"):
        raise SelfUpgradeRunnerError("intent authorization was not consumed by candidate generation")
    if request.get("intent_auth_id") != session.get("intent_auth_id"):
        raise SelfUpgradeRunnerError("intent authorization id mismatch")
    if request.get("candidate_auth_id") != session.get("candidate_auth_id"):
        raise SelfUpgradeRunnerError("candidate authorization id mismatch")
    if request.get("candidate_binding") != session.get("candidate_binding"):
        raise SelfUpgradeRunnerError("candidate binding mismatch")
    if request.get("changed_files") != session.get("changed_file_records"):
        raise SelfUpgradeRunnerError("changed-file manifest mismatch")
    return payload, session


def verify_candidate(request: dict[str, Any], session: dict[str, Any]) -> None:
    if not CANDIDATE_REPO.is_dir():
        raise SelfUpgradeRunnerError(f"candidate repository missing: {CANDIDATE_REPO}")
    manifest = tree_manifest(CANDIDATE_REPO)
    digest = json_digest(manifest)
    if digest != str(request.get("candidate_digest", "")):
        raise SelfUpgradeRunnerError("candidate tree changed after owner approval")
    binding = session.get("candidate_binding")
    if not isinstance(binding, dict):
        raise SelfUpgradeRunnerError("candidate binding missing")
    if binding.get("candidate_tree_sha256") != digest:
        raise SelfUpgradeRunnerError("candidate binding digest mismatch")

    allowed = session.get("plan", {}).get("allowed_paths", [])
    if not isinstance(allowed, list):
        raise SelfUpgradeRunnerError("session allowed_paths invalid")
    for record in request.get("changed_files") or []:
        if not isinstance(record, dict):
            raise SelfUpgradeRunnerError("changed-file record must be an object")
        relative = authorized_upgrade.validate_repo_path(record.get("path"), allowed)
        candidate = CANDIDATE_REPO / relative
        if not candidate.is_file() or candidate.is_symlink():
            raise SelfUpgradeRunnerError(f"candidate file missing or symlinked: {relative}")
        body = candidate.read_text(encoding="utf-8")
        authorized_upgrade.scan_redlines(relative, body)
        if file_sha256(candidate) != str(record.get("after_sha256", "")):
            raise SelfUpgradeRunnerError(f"candidate file hash mismatch: {relative}")

        target = target_path(relative)
        before = str(record.get("before_sha256", ""))
        if before:
            if not target.is_file() or file_sha256(target) != before:
                raise SelfUpgradeRunnerError(
                    f"target changed since candidate baseline; refusing stale overwrite: {relative}"
                )
        elif target.exists():
            raise SelfUpgradeRunnerError(f"candidate expected a new file but target exists: {relative}")


def rerun_tests(session: dict[str, Any]) -> dict[str, Any]:
    runner = ROOT / "scripts" / "run_authorized_upgrade_tests.py"
    baseline = Path(str(session.get("baseline_manifest_path", "")))
    if not runner.is_file() or not baseline.is_file():
        raise SelfUpgradeRunnerError("trusted test runner or baseline manifest missing")
    try:
        process = subprocess.run(
            [
                sys.executable,
                str(runner),
                "--candidate-repo",
                str(CANDIDATE_REPO),
                "--baseline-manifest",
                str(baseline),
                "--timeout",
                "600",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
        )
    except subprocess.TimeoutExpired as exc:
        raise SelfUpgradeRunnerError("candidate revalidation timed out") from exc
    output = "\n".join(part for part in (process.stdout, process.stderr) if part)[-MAX_OUTPUT:]
    if process.returncode != 0:
        raise SelfUpgradeRunnerError("candidate revalidation failed:\n" + output[-8000:])
    try:
        value = json.loads(process.stdout or "{}")
    except json.JSONDecodeError:
        value = {"status": "passed", "output": output}
    return value if isinstance(value, dict) else {"status": "passed", "output": output}


def backup_targets(request_id: str, records: list[dict[str, Any]]) -> tuple[Path, dict[str, Any]]:
    directory = queue_paths()["backups"] / request_id
    directory.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, Any] = {"request_id": request_id, "created_at": now_iso(), "files": []}
    for record in records:
        relative = str(record["path"])
        source = target_path(relative)
        entry = {"path": relative, "existed": source.is_file()}
        if source.is_file():
            destination = directory / "files" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            entry["sha256"] = file_sha256(source)
        manifest["files"].append(entry)
    atomic_json(directory / "manifest.json", manifest)
    return directory, manifest


def atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}-", suffix=".upgrade", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as handle, source.open("rb") as input_handle:
            shutil.copyfileobj(input_handle, handle)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, source.stat().st_mode & 0o777)
        except OSError:
            pass
        os.replace(temporary, destination)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def rollback(backup_dir: Path, manifest: dict[str, Any]) -> None:
    for entry in reversed(manifest.get("files") or []):
        relative = str(entry.get("path", ""))
        target = target_path(relative)
        if entry.get("existed"):
            backup = backup_dir / "files" / relative
            if backup.is_file():
                atomic_copy(backup, target)
        else:
            try:
                target.unlink()
            except FileNotFoundError:
                pass


def apply_files(request: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
    records = [item for item in request.get("changed_files") or [] if isinstance(item, dict)]
    backup_dir, backup_manifest = backup_targets(str(request["id"]), records)
    applied: list[str] = []
    try:
        for record in records:
            relative = str(record["path"])
            source = CANDIDATE_REPO / relative
            destination = target_path(relative)
            atomic_copy(source, destination)
            if file_sha256(destination) != str(record.get("after_sha256", "")):
                raise SelfUpgradeRunnerError(f"post-write hash mismatch: {relative}")
            applied.append(relative)
    except Exception:
        rollback(backup_dir, backup_manifest)
        raise

    restart_required = any(
        not (
            path.startswith("app/skills/")
            or path.startswith("app/tests/")
            or path.startswith("docs/")
            or path in {"README.md", "Makefile"}
        )
        for path in applied
    )
    return {
        "applied": applied,
        "backup_dir": str(backup_dir),
        "restart_required": restart_required,
        "hot_reloadable_skills": [
            Path(path).stem
            for path in applied
            if path.startswith("app/skills/") and path.endswith(".py")
        ],
    }


def process_request(path: Path) -> str:
    request = read_json(path)
    if request is None:
        return "invalid"
    request_id = str(request.get("id", ""))
    paths = queue_paths()
    result_path = paths["results"] / f"{request_id}.json"
    if result_path.is_file():
        return "done"
    lock = paths["locks"] / f"{request_id}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    except FileExistsError:
        return "locked"

    try:
        _payload, session = validate_request(request)
        candidate_auth_id = str(request["candidate_auth_id"])
        candidate_binding = session["candidate_binding"]
        state = permissions.check_auth(candidate_auth_id, expected_binding=candidate_binding)
        if state == permissions.STATUS_PENDING:
            return "pending"
        if state != permissions.STATUS_APPROVED:
            raise SelfUpgradeRunnerError(f"candidate authorization is not approved: {state}")
        verify_candidate(request, session)
        test_report = rerun_tests(session)
        if not permissions.consume_auth(candidate_auth_id, expected_binding=candidate_binding):
            raise SelfUpgradeRunnerError("candidate authorization could not be consumed")
        applied = apply_files(request, session)
        result = {
            "schema_version": 1,
            "id": request_id,
            "status": "succeeded",
            "session_id": session["id"],
            "finished_at": now_iso(),
            "changed_files": applied["applied"],
            "backup_dir": applied["backup_dir"],
            "restart_required": applied["restart_required"],
            "hot_reloadable_skills": applied["hot_reloadable_skills"],
            "test_report": test_report,
        }
        atomic_json(result_path, result, exclusive=True)
        audit("succeeded", f"{request_id} files={','.join(applied['applied'])}")
        return "succeeded"
    except Exception as exc:
        result = {
            "schema_version": 1,
            "id": request_id,
            "status": "failed",
            "finished_at": now_iso(),
            "error": f"{type(exc).__name__}: {exc}",
        }
        try:
            atomic_json(result_path, result, exclusive=True)
        except FileExistsError:
            pass
        audit("failed", f"{request_id} {type(exc).__name__}: {exc}")
        return "failed"
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def run_once() -> dict[str, int]:
    paths = queue_paths()
    paths["requests"].mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for path in sorted(paths["requests"].glob("self-upgrade-*.json")):
        state = process_request(path)
        counts[state] = counts.get(state, 0) + 1
    return counts


def watch(interval: float) -> None:
    audit("runner_started", f"target={TARGET_ROOT} candidate={CANDIDATE_REPO}")
    while True:
        run_once()
        time.sleep(max(0.2, float(interval)))


def main() -> int:
    parser = argparse.ArgumentParser(description="Agenelf isolated owner-authorized self-upgrade runner")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    try:
        if args.once:
            print(json.dumps(run_once(), ensure_ascii=False))
        else:
            watch(args.interval)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"self-upgrade-runner failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
