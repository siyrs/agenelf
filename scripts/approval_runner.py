#!/usr/bin/env python3
"""Deterministic broker for signed interactive owner approval commands."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("AGENELF_ROOT", Path(__file__).resolve().parents[1])).resolve()
APP_DIR = ROOT / ("app-fork" if (ROOT / "app-fork").is_dir() else "app")
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from core import owner_approval  # noqa: E402


class ApprovalRunner:
    def __init__(self, root: Path = ROOT, key: bytes | str | None = None):
        self.root = root.resolve()
        self.paths = owner_approval.approval_paths(self.root)
        self.key = key

    def audit(self, event: str, detail: str) -> None:
        path = self.paths["audit"]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"[{owner_approval.now_iso()}] [{event}] {detail[:2000]}\n")
        except OSError:
            pass

    def _write_failure(self, command_id: str, exc: Exception) -> None:
        result = {
            "schema_version": 1,
            "id": command_id,
            "status": "failed",
            "finished_at": owner_approval.now_iso(),
            "error": f"{type(exc).__name__}: {exc}"[:2000],
        }
        try:
            owner_approval.write_command_result(command_id, result, root=self.root)
        except FileExistsError:
            pass

    def process(self, command_path: Path) -> str:
        command = owner_approval.read_json(command_path)
        if command is None:
            return "invalid"
        command_id = str(command.get("id", ""))
        result_path = self.paths["command_results"] / f"{command_id}.json"
        if result_path.exists():
            return "done"
        lock_path = self.paths["command_locks"] / f"{command_id}.lock"
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        except FileExistsError:
            return "locked"
        try:
            result = owner_approval.process_owner_command(
                command, root=self.root, key=self.key
            )
            owner_approval.write_command_result(command_id, result, root=self.root)
            self.audit(
                "owner_decision_applied",
                f"command={command_id} request={result.get('request_id')} "
                f"decision={result.get('decision', {}).get('decision')}",
            )
            return "succeeded"
        except Exception as exc:
            self._write_failure(command_id, exc)
            self.audit(
                "owner_decision_failed",
                f"command={command_id} error={type(exc).__name__}: {exc}",
            )
            return "failed"
        finally:
            try:
                lock_path.unlink()
            except OSError:
                pass

    def run_once(self) -> dict[str, int]:
        self.paths["commands"].mkdir(parents=True, exist_ok=True)
        counts: dict[str, int] = {}
        for path in sorted(self.paths["commands"].glob("owner-decision-*.json")):
            state = self.process(path)
            counts[state] = counts.get(state, 0) + 1
        return counts

    def watch(self, interval: float = 0.25) -> None:
        self.audit("approval_runner_started", f"root={self.root}")
        while True:
            self.run_once()
            time.sleep(max(0.05, interval))


def main() -> int:
    parser = argparse.ArgumentParser(description="Agenelf owner approval runner")
    parser.add_argument("--once", action="store_true", help="process queue once and exit")
    parser.add_argument("--interval", type=float, default=0.25)
    args = parser.parse_args()
    try:
        runner = ApprovalRunner()
        if args.once:
            print(json.dumps(runner.run_once(), ensure_ascii=False))
        else:
            runner.watch(args.interval)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"approval-runner 启动失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
