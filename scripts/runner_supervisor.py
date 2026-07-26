#!/usr/bin/env python3
"""Supervise one deterministic runner and publish a bounded liveness heartbeat.

The supervisor does not interpret a shell command.  Docker Compose supplies a fixed argv
sequence after ``--``.  A heartbeat contains only process/liveness metadata and never
contains request parameters, credentials, environment values, model output or command
stdout/stderr.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def runtime_root(root: str | Path | None = None) -> Path:
    if root is not None:
        return Path(root).resolve()
    configured = os.environ.get("AGENELF_ROOT", "").strip()
    return Path(configured).resolve() if configured else Path(__file__).resolve().parents[1]


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=".tmp", dir=path.parent, text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


class HeartbeatWriter:
    """Write one atomic heartbeat file for a named runner."""

    def __init__(
        self,
        name: str,
        *,
        root: str | Path | None = None,
        heartbeat_interval: float = 1.0,
    ) -> None:
        normalized = str(name or "").strip().lower()
        if not _NAME_RE.fullmatch(normalized):
            raise ValueError(f"非法 runner 名称：{name!r}")
        self.name = normalized
        self.root = runtime_root(root)
        self.path = self.root / "data" / "runner-health" / f"{self.name}.json"
        self.interval = max(0.1, min(float(heartbeat_interval), 60.0))
        self.started_at = now_iso()
        self.sequence = 0

    @property
    def expires_after_seconds(self) -> float:
        return round(max(5.0, self.interval * 4.0 + 2.0), 2)

    def write(
        self,
        status: str,
        *,
        child_pid: int | None = None,
        exit_code: int | None = None,
        error: str = "",
    ) -> dict[str, Any]:
        self.sequence += 1
        value: dict[str, Any] = {
            "schema_version": 1,
            "runner": self.name,
            "status": str(status),
            "supervisor_pid": os.getpid(),
            "child_pid": child_pid,
            "started_at": self.started_at,
            "heartbeat_at": now_iso(),
            "sequence": self.sequence,
            "expires_after_seconds": self.expires_after_seconds,
            "runtime_source": os.environ.get("AGENELF_RUNTIME_SOURCE", "unknown")[:64],
        }
        if exit_code is not None:
            value["exit_code"] = int(exit_code)
        if error:
            value["error"] = " ".join(str(error).split())[:1000]
        _atomic_json(self.path, value)
        return value


def _terminate_child(process: subprocess.Popen[Any], grace_seconds: float = 5.0) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=max(0.1, grace_seconds))
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=max(0.1, grace_seconds))


def supervise(
    name: str,
    command: Sequence[str],
    *,
    root: str | Path | None = None,
    heartbeat_interval: float = 1.0,
    popen_factory: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
) -> int:
    """Run ``command`` without a shell and return its exit code."""

    argv = [str(item) for item in command]
    if not argv or not argv[0].strip():
        raise ValueError("runner command 不能为空")
    if any("\x00" in item or "\n" in item for item in argv):
        raise ValueError("runner command 含非法控制字符")

    writer = HeartbeatWriter(
        name,
        root=root,
        heartbeat_interval=heartbeat_interval,
    )
    writer.write("starting")
    process: subprocess.Popen[Any] | None = None
    previous_handlers: dict[int, Any] = {}

    def stop_handler(signum: int, _frame: Any) -> None:
        if process is None:
            return
        try:
            writer.write("stopping", child_pid=process.pid)
        finally:
            _terminate_child(process)

    try:
        process = popen_factory(argv, shell=False)
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, stop_handler)

        while True:
            exit_code = process.poll()
            if exit_code is not None:
                status = "stopped" if exit_code == 0 else "failed"
                writer.write(status, child_pid=process.pid, exit_code=exit_code)
                return int(exit_code)
            writer.write("running", child_pid=process.pid)
            time.sleep(writer.interval)
    except BaseException as exc:
        child_pid = process.pid if process is not None else None
        try:
            writer.write(
                "failed",
                child_pid=child_pid,
                exit_code=process.poll() if process is not None else None,
                error=f"{type(exc).__name__}: {exc}",
            )
        except OSError:
            pass
        if process is not None:
            _terminate_child(process)
        raise
    finally:
        if previous_handlers:
            for signum, previous in previous_handlers.items():
                signal.signal(signum, previous)


def main() -> int:
    parser = argparse.ArgumentParser(description="Agenelf deterministic runner supervisor")
    parser.add_argument("--name", required=True)
    parser.add_argument("--heartbeat-interval", type=float, default=1.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    try:
        return supervise(
            args.name,
            command,
            heartbeat_interval=args.heartbeat_interval,
        )
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(
            f"runner-supervisor 启动失败：{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
