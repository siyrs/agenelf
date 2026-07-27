#!/usr/bin/env python3
"""Supervise one deterministic runner and recover abandoned queue locks safely.

Docker Compose supplies a fixed argv sequence after ``--``. The supervisor never
interprets a shell command. It owns an exclusive per-runner lease, publishes a bounded
liveness heartbeat, and removes queue ``.lock`` files only after proving that no other
supervisor for the same runner is active.

Heartbeats and leases contain only process/liveness metadata. They never contain request
parameters, credentials, environment values, model output, command argv, stdout or
stderr.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
_QUEUE_LOCK_DIRS = {
    "ops-runner": "data/ops-locks",
    "approval-runner": "data/approval-locks",
    "self-upgrade-runner": "data/self-upgrade-locks",
    "validation-runner": "data/validation-locks",
    "repair-runner": "data/repair-locks",
}
_DEFAULT_LEASE_STALE_SECONDS = 15.0


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat(timespec="seconds")


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


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _parse_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _bounded_float(value: object, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _pid_namespace() -> str:
    try:
        return os.readlink("/proc/self/ns/pid")[:128]
    except OSError:
        return f"platform:{sys.platform}"


def _process_alive(pid: object) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class SupervisorLeaseError(RuntimeError):
    """Another live supervisor already owns this deterministic runner."""


class SupervisorLease:
    """Atomic directory lease that prevents duplicate supervisors and enables recovery."""

    def __init__(
        self,
        name: str,
        *,
        root: str | Path | None = None,
        stale_after_seconds: float = _DEFAULT_LEASE_STALE_SECONDS,
    ) -> None:
        normalized = str(name or "").strip().lower()
        if not _NAME_RE.fullmatch(normalized):
            raise ValueError(f"非法 runner 名称：{name!r}")
        self.name = normalized
        self.root = runtime_root(root)
        self.directory = self.root / "data" / "runner-health" / f"{self.name}.supervisor"
        self.owner_path = self.directory / "owner.json"
        self.instance_id = uuid.uuid4().hex
        self.pid_namespace = _pid_namespace()
        self.supervisor_pid = os.getpid()
        self.started_at = now_iso()
        self.stale_after_seconds = _bounded_float(
            stale_after_seconds,
            _DEFAULT_LEASE_STALE_SECONDS,
            2.0,
            300.0,
        )
        self.acquired = False
        self.reclaimed_previous_lease = False

    def _payload(self, status: str, child_pid: int | None = None) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "runner": self.name,
            "instance_id": self.instance_id,
            "status": str(status),
            "supervisor_pid": self.supervisor_pid,
            "child_pid": child_pid,
            "pid_namespace": self.pid_namespace,
            "started_at": self.started_at,
            "heartbeat_at": now_iso(),
            "stale_after_seconds": self.stale_after_seconds,
        }

    def _owner_is_live(self, owner: dict[str, Any] | None) -> bool:
        if not owner:
            return False
        heartbeat_at = _parse_time(owner.get("heartbeat_at"))
        if heartbeat_at is None:
            return False
        same_namespace = str(owner.get("pid_namespace", "")) == self.pid_namespace
        if same_namespace:
            # A live PID in the same namespace wins even if its heartbeat is delayed.
            # This favors at-most-once execution over automatic takeover of a hung peer.
            return _process_alive(owner.get("supervisor_pid"))
        # PIDs cannot be inspected across container namespaces. A fresh shared-volume
        # heartbeat therefore proves another container may still be active. Takeover is
        # allowed only after that heartbeat expires.
        age = max(0.0, (now_utc() - heartbeat_at).total_seconds())
        return age <= self.stale_after_seconds

    def acquire(self) -> None:
        self.directory.parent.mkdir(parents=True, exist_ok=True)
        for _attempt in range(8):
            try:
                self.directory.mkdir(mode=0o700)
            except FileExistsError:
                owner = _read_json(self.owner_path)
                if self._owner_is_live(owner):
                    raise SupervisorLeaseError(
                        f"{self.name} 已由另一个活动 supervisor 持有"
                    )
                quarantine = self.directory.with_name(
                    f".{self.directory.name}.stale-{uuid.uuid4().hex[:12]}"
                )
                try:
                    os.replace(self.directory, quarantine)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise SupervisorLeaseError(
                        f"无法回收 {self.name} 的陈旧 supervisor 租约：{exc}"
                    ) from exc
                shutil.rmtree(quarantine, ignore_errors=True)
                self.reclaimed_previous_lease = True
                continue
            _atomic_json(self.owner_path, self._payload("starting"))
            self.acquired = True
            return
        raise SupervisorLeaseError(f"无法获取 {self.name} supervisor 租约")

    def refresh(self, status: str, child_pid: int | None = None) -> None:
        if not self.acquired:
            return
        owner = _read_json(self.owner_path)
        if not owner or owner.get("instance_id") != self.instance_id:
            raise SupervisorLeaseError(f"{self.name} supervisor 租约所有权已丢失")
        _atomic_json(self.owner_path, self._payload(status, child_pid))

    def release(self) -> None:
        if not self.acquired:
            return
        owner = _read_json(self.owner_path)
        if owner and owner.get("instance_id") == self.instance_id:
            shutil.rmtree(self.directory, ignore_errors=True)
        self.acquired = False


def reclaim_abandoned_queue_locks(
    name: str,
    *,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Remove only fixed queue lock files after the caller owns the supervisor lease."""

    normalized = str(name or "").strip().lower()
    relative = _QUEUE_LOCK_DIRS.get(normalized)
    if relative is None:
        return {"lock_dir": "", "reclaimed": 0, "skipped": 0}
    base = runtime_root(root)
    directory = (base / relative).resolve()
    data_root = (base / "data").resolve()
    if not directory.is_relative_to(data_root):
        raise ValueError("queue lock 目录逃逸出 data 根目录")
    directory.mkdir(parents=True, exist_ok=True)
    reclaimed = 0
    skipped = 0
    for path in sorted(directory.glob("*.lock")):
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(mode):
            skipped += 1
            continue
        try:
            path.unlink()
            reclaimed += 1
        except FileNotFoundError:
            continue
        except OSError:
            skipped += 1
    return {
        "lock_dir": relative,
        "reclaimed": reclaimed,
        "skipped": skipped,
    }


class HeartbeatWriter:
    """Write one atomic heartbeat file for a named runner."""

    def __init__(
        self,
        name: str,
        *,
        root: str | Path | None = None,
        heartbeat_interval: float = 1.0,
        instance_id: str = "",
        lock_recovery: dict[str, Any] | None = None,
        reclaimed_previous_lease: bool = False,
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
        self.instance_id = str(instance_id)[:64]
        raw_recovery = lock_recovery if isinstance(lock_recovery, dict) else {}
        self.lock_recovery = {
            "lock_dir": str(raw_recovery.get("lock_dir", ""))[:128],
            "reclaimed": max(0, int(raw_recovery.get("reclaimed", 0) or 0)),
            "skipped": max(0, int(raw_recovery.get("skipped", 0) or 0)),
        }
        self.reclaimed_previous_lease = bool(reclaimed_previous_lease)

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
            "schema_version": 2,
            "runner": self.name,
            "status": str(status),
            "supervisor_pid": os.getpid(),
            "child_pid": child_pid,
            "started_at": self.started_at,
            "heartbeat_at": now_iso(),
            "sequence": self.sequence,
            "expires_after_seconds": self.expires_after_seconds,
            "runtime_source": os.environ.get("AGENELF_RUNTIME_SOURCE", "unknown")[:64],
            "instance_id": self.instance_id,
            "pid_namespace": _pid_namespace(),
            "lock_recovery": dict(self.lock_recovery),
            "reclaimed_previous_lease": self.reclaimed_previous_lease,
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
    lease_stale_seconds: float = _DEFAULT_LEASE_STALE_SECONDS,
    popen_factory: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
) -> int:
    """Run ``command`` without a shell and return its exit code."""

    argv = [str(item) for item in command]
    if not argv or not argv[0].strip():
        raise ValueError("runner command 不能为空")
    if any("\x00" in item or "\n" in item for item in argv):
        raise ValueError("runner command 含非法控制字符")

    lease = SupervisorLease(
        name,
        root=root,
        stale_after_seconds=lease_stale_seconds,
    )
    lease.acquire()
    recovery = reclaim_abandoned_queue_locks(name, root=root)
    writer = HeartbeatWriter(
        name,
        root=root,
        heartbeat_interval=heartbeat_interval,
        instance_id=lease.instance_id,
        lock_recovery=recovery,
        reclaimed_previous_lease=lease.reclaimed_previous_lease,
    )
    writer.write("starting")
    lease.refresh("starting")
    process: subprocess.Popen[Any] | None = None
    previous_handlers: dict[int, Any] = {}

    def publish(status: str, *, exit_code: int | None = None, error: str = "") -> None:
        child_pid = process.pid if process is not None else None
        writer.write(status, child_pid=child_pid, exit_code=exit_code, error=error)
        lease.refresh(status, child_pid)

    def stop_handler(signum: int, _frame: Any) -> None:
        del signum
        if process is None:
            return
        try:
            publish("stopping")
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
                publish(status, exit_code=int(exit_code))
                return int(exit_code)
            publish("running")
            time.sleep(writer.interval)
    except BaseException as exc:
        exit_code = process.poll() if process is not None else None
        try:
            publish(
                "failed",
                exit_code=exit_code,
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception:
            pass
        if process is not None:
            _terminate_child(process)
        raise
    finally:
        if previous_handlers:
            for signum, previous in previous_handlers.items():
                signal.signal(signum, previous)
        lease.release()


def main() -> int:
    parser = argparse.ArgumentParser(description="Agenelf deterministic runner supervisor")
    parser.add_argument("--name", required=True)
    parser.add_argument("--heartbeat-interval", type=float, default=1.0)
    parser.add_argument(
        "--lease-stale-seconds",
        type=float,
        default=_bounded_float(
            os.environ.get("AGENELF_SUPERVISOR_LEASE_STALE_SECONDS", "15"),
            _DEFAULT_LEASE_STALE_SECONDS,
            2.0,
            300.0,
        ),
    )
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
            lease_stale_seconds=args.lease_stale_seconds,
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
