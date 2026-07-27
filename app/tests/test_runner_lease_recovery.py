from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "agenelf_runner_supervisor_recovery",
    PROJECT_ROOT / "scripts" / "runner_supervisor.py",
)
supervisor = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = supervisor
SPEC.loader.exec_module(supervisor)


class FakeProcess:
    def __init__(self, exit_code: int = 0, running_polls: int = 0) -> None:
        self.pid = 4321
        self.exit_code = exit_code
        self.running_polls = running_polls
        self.poll_calls = 0

    def poll(self):
        self.poll_calls += 1
        return None if self.poll_calls <= self.running_polls else self.exit_code

    def terminate(self) -> None:
        self.running_polls = 0

    def kill(self) -> None:
        self.running_polls = 0

    def wait(self, timeout=None):
        del timeout
        return self.exit_code


class RunnerLeaseRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _heartbeat(self, name: str) -> dict:
        return json.loads(
            (self.root / "data" / "runner-health" / f"{name}.json").read_text(
                encoding="utf-8"
            )
        )

    def test_abandoned_lock_is_removed_before_child_starts(self) -> None:
        lock_dir = self.root / "data" / "ops-locks"
        lock_dir.mkdir(parents=True)
        lock = lock_dir / "op-dead.lock"
        lock.write_text("", encoding="utf-8")

        def factory(argv, shell=False):
            self.assertFalse(shell)
            self.assertFalse(lock.exists())
            return FakeProcess()

        code = supervisor.supervise(
            "ops-runner",
            ["python", "runner.py"],
            root=self.root,
            heartbeat_interval=0.1,
            popen_factory=factory,
        )

        self.assertEqual(code, 0)
        heartbeat = self._heartbeat("ops-runner")
        self.assertEqual(heartbeat["lock_recovery"]["reclaimed"], 1)
        self.assertEqual(heartbeat["lock_recovery"]["lock_dir"], "data/ops-locks")
        self.assertNotIn("argv", heartbeat)
        self.assertNotIn("environment", heartbeat)
        self.assertNotIn("command", heartbeat)

    def test_live_duplicate_supervisor_is_rejected(self) -> None:
        lease = supervisor.SupervisorLease("approval-runner", root=self.root)
        lease.acquire()
        invoked = False

        def factory(argv, shell=False):
            del argv, shell
            nonlocal invoked
            invoked = True
            return FakeProcess()

        try:
            with self.assertRaisesRegex(
                supervisor.SupervisorLeaseError,
                "活动 supervisor",
            ):
                supervisor.supervise(
                    "approval-runner",
                    ["python", "runner.py"],
                    root=self.root,
                    popen_factory=factory,
                )
        finally:
            lease.release()
        self.assertFalse(invoked)

    def test_stale_namespace_lease_is_reclaimed(self) -> None:
        lease_dir = (
            self.root
            / "data"
            / "runner-health"
            / "validation-runner.supervisor"
        )
        lease_dir.mkdir(parents=True)
        (lease_dir / "owner.json").write_text(
            json.dumps(
                {
                    "instance_id": "old",
                    "pid_namespace": "pid:[old-container]",
                    "supervisor_pid": os.getpid(),
                    "heartbeat_at": supervisor.now_iso(),
                }
            ),
            encoding="utf-8",
        )

        code = supervisor.supervise(
            "validation-runner",
            ["python", "runner.py"],
            root=self.root,
            popen_factory=lambda argv, shell=False: FakeProcess(),
        )

        self.assertEqual(code, 0)
        self.assertTrue(self._heartbeat("validation-runner")["reclaimed_previous_lease"])

    def test_non_regular_lock_entries_are_not_removed(self) -> None:
        lock_dir = self.root / "data" / "repair-locks"
        lock_dir.mkdir(parents=True)
        (lock_dir / "nested.lock").mkdir()

        result = supervisor.reclaim_abandoned_queue_locks(
            "repair-runner",
            root=self.root,
        )

        self.assertEqual(result["reclaimed"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertTrue((lock_dir / "nested.lock").is_dir())

    def test_all_long_running_runners_have_fixed_lock_directories(self) -> None:
        self.assertEqual(
            set(supervisor._QUEUE_LOCK_DIRS),
            {
                "ops-runner",
                "approval-runner",
                "self-upgrade-runner",
                "validation-runner",
                "repair-runner",
            },
        )
        for relative in supervisor._QUEUE_LOCK_DIRS.values():
            self.assertTrue(relative.startswith("data/"))
            self.assertNotIn("..", Path(relative).parts)


if __name__ == "__main__":
    unittest.main(verbosity=2)
