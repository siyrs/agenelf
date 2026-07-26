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
    "agenelf_runner_supervisor",
    PROJECT_ROOT / "scripts" / "runner_supervisor.py",
)
supervisor = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = supervisor
SPEC.loader.exec_module(supervisor)


class FakeProcess:
    def __init__(self, exit_code: int = 0, running_polls: int = 1) -> None:
        self.pid = 4321
        self.exit_code = exit_code
        self.running_polls = running_polls
        self.poll_calls = 0
        self.terminated = False
        self.killed = False

    def poll(self):
        self.poll_calls += 1
        return None if self.poll_calls <= self.running_polls else self.exit_code

    def terminate(self) -> None:
        self.terminated = True
        self.running_polls = 0

    def kill(self) -> None:
        self.killed = True
        self.running_polls = 0

    def wait(self, timeout=None):
        del timeout
        return self.exit_code


class RunnerSupervisorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_source = os.environ.get("AGENELF_RUNTIME_SOURCE")
        os.environ["AGENELF_RUNTIME_SOURCE"] = "app-bind"

    def tearDown(self) -> None:
        if self.old_source is None:
            os.environ.pop("AGENELF_RUNTIME_SOURCE", None)
        else:
            os.environ["AGENELF_RUNTIME_SOURCE"] = self.old_source
        self.tmp.cleanup()

    def _heartbeat(self, name: str) -> dict:
        path = self.root / "data" / "runner-health" / f"{name}.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_supervise_uses_argv_without_shell_and_writes_terminal_heartbeat(self) -> None:
        calls = []
        fake = FakeProcess(exit_code=0, running_polls=1)

        def factory(argv, shell=False):
            calls.append((list(argv), shell))
            return fake

        code = supervisor.supervise(
            "ops-runner",
            ["python", "/agenelf/scripts/unified_ops_runner.py", "--interval", "1"],
            root=self.root,
            heartbeat_interval=0.1,
            popen_factory=factory,
        )

        self.assertEqual(code, 0)
        self.assertEqual(
            calls,
            [
                (
                    [
                        "python",
                        "/agenelf/scripts/unified_ops_runner.py",
                        "--interval",
                        "1",
                    ],
                    False,
                )
            ],
        )
        heartbeat = self._heartbeat("ops-runner")
        self.assertEqual(heartbeat["status"], "stopped")
        self.assertEqual(heartbeat["exit_code"], 0)
        self.assertEqual(heartbeat["runtime_source"], "app-bind")
        self.assertGreaterEqual(heartbeat["sequence"], 3)
        self.assertNotIn("command", heartbeat)
        self.assertNotIn("argv", heartbeat)
        self.assertNotIn("environment", heartbeat)

    def test_nonzero_child_exit_is_reported_without_hiding_failure(self) -> None:
        fake = FakeProcess(exit_code=7, running_polls=0)
        code = supervisor.supervise(
            "validation-runner",
            ["python", "runner.py"],
            root=self.root,
            heartbeat_interval=0.1,
            popen_factory=lambda argv, shell=False: fake,
        )
        self.assertEqual(code, 7)
        heartbeat = self._heartbeat("validation-runner")
        self.assertEqual(heartbeat["status"], "failed")
        self.assertEqual(heartbeat["exit_code"], 7)

    def test_heartbeat_writer_rejects_untrusted_names(self) -> None:
        for value in ("../escape", "runner/name", "", "A SPACE"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    supervisor.HeartbeatWriter(value, root=self.root)

    def test_command_control_characters_are_rejected_before_process_start(self) -> None:
        invoked = False

        def factory(argv, shell=False):
            nonlocal invoked
            invoked = True
            raise AssertionError("must not start")

        with self.assertRaises(ValueError):
            supervisor.supervise(
                "repair-runner",
                ["python", "bad\nargument"],
                root=self.root,
                popen_factory=factory,
            )
        self.assertFalse(invoked)


if __name__ == "__main__":
    unittest.main(verbosity=2)
