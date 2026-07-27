from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from core import operations

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("agenelf_ops_runner", ROOT / "scripts" / "ops_runner.py")
ops_runner = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = ops_runner
SPEC.loader.exec_module(ops_runner)


class FakeSession:
    commands: list[str] = []
    writes: list[tuple[str, str]] = []
    fail_contains: str | None = None

    def __init__(self, profile, secrets_root):
        self.profile = profile
        self.secrets_root = secrets_root

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def run(self, command: str, timeout: int = 120):
        self.__class__.commands.append(command)
        code = 1 if self.__class__.fail_contains and self.__class__.fail_contains in command else 0
        return ops_runner.CommandResult(command, code, "ok\n" if code == 0 else "", "boom\n" if code else "")

    def write_text(self, remote_path: str, content: str):
        self.__class__.writes.append((remote_path, content))


class OpsRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_root = os.environ.get("AGENELF_ROOT")
        os.environ["AGENELF_ROOT"] = str(self.root)
        self.servers_file = self.root / "servers.yaml"
        self.servers_file.write_text(
            """servers:
  primary:
    host: 127.0.0.1
    username: agenelf
    managed_root: /srv/agenelf
    docker_command: docker
    allowed_operations: [inspect, docker_ps, service_status, apt_update, compose_deploy, service_restart, docker_install]
    allowed_services: [nginx]
    allowed_bind_roots: [/srv/data]
""",
            encoding="utf-8",
        )
        FakeSession.commands = []
        FakeSession.writes = []
        FakeSession.fail_contains = None
        self.runner = ops_runner.OpsRunner(
            root=self.root,
            servers_file=self.servers_file,
            secrets_root=self.root / "secrets",
            session_factory=FakeSession,
        )

    def tearDown(self):
        if self.old_root is None:
            os.environ.pop("AGENELF_ROOT", None)
        else:
            os.environ["AGENELF_ROOT"] = self.old_root
        self.tmp.cleanup()

    def _approve(self, request: dict, decision="approve", fingerprint=None):
        path = self.root / "data" / "auth-decisions" / f"{request['id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "request_id": request["id"],
                    "decision": decision,
                    "fingerprint": fingerprint or request["fingerprint"],
                    "expires_at": (
                        datetime.now().astimezone() + timedelta(minutes=5)
                    ).isoformat(timespec="seconds"),
                }
            ),
            encoding="utf-8",
        )

    def test_read_operation_executes_without_human_decision(self):
        request = operations.submit_operation(
            "server.operations", "inspect", "primary", {}, "read", "巡检", root=self.root
        )
        counts = self.runner.run_once()
        self.assertEqual(counts.get("succeeded"), 1)
        state = operations.get_operation(request["id"], root=self.root)
        self.assertEqual(state["status"], "succeeded")
        self.assertTrue(FakeSession.commands)

    def test_change_waits_for_exact_approval(self):
        request = operations.submit_operation(
            "server.operations", "apt_update", "primary", {}, "change", "apt", root=self.root
        )
        self.assertEqual(self.runner.run_once().get("pending"), 1)
        self.assertFalse((self.root / "data" / "ops-results" / f"{request['id']}.json").exists())
        self._approve(request)
        self.assertEqual(self.runner.run_once().get("succeeded"), 1)
        self.assertIn("sudo -n apt-get update", FakeSession.commands[-1])

    def test_wrong_fingerprint_is_blocked(self):
        request = operations.submit_operation(
            "server.operations", "apt_update", "primary", {}, "change", "apt", root=self.root
        )
        self._approve(request, fingerprint="0" * 64)
        self.assertEqual(self.runner.run_once().get("blocked"), 1)
        state = operations.get_operation(request["id"], root=self.root)
        self.assertEqual(state["status"], "blocked")
        self.assertFalse(FakeSession.commands)

    def test_agent_cannot_downgrade_change_to_read(self):
        request = operations.submit_operation(
            "server.operations", "apt_update", "primary", {}, "change", "apt", root=self.root
        )
        request_path = self.root / "data" / "ops-requests" / f"{request['id']}.json"
        data = json.loads(request_path.read_text(encoding="utf-8"))
        data["risk"] = "read"
        request_path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.runner.run_once().get("failed"), 1)
        self.assertFalse(FakeSession.commands)

    def test_tampered_request_is_failed_before_ssh(self):
        request = operations.submit_operation(
            "server.operations", "apt_update", "primary", {}, "change", "apt", root=self.root
        )
        request_path = self.root / "data" / "ops-requests" / f"{request['id']}.json"
        data = json.loads(request_path.read_text(encoding="utf-8"))
        data["target"] = "other"
        request_path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.runner.run_once().get("failed"), 1)
        self.assertFalse(FakeSession.commands)

    def test_compose_deploy_writes_validated_file(self):
        compose = "services:\n  web:\n    image: nginx:alpine\n"
        request = operations.submit_operation(
            "server.operations",
            "compose_deploy",
            "primary",
            {"project": "demo", "compose_yaml": compose, "pull": False},
            "change",
            "deploy",
            root=self.root,
        )
        self._approve(request)
        self.assertEqual(self.runner.run_once().get("succeeded"), 1)
        self.assertEqual(len(FakeSession.writes), 1)
        self.assertIn("/srv/agenelf/demo/.compose.", FakeSession.writes[0][0])
        self.assertIn("compose up -d --remove-orphans", "\n".join(FakeSession.commands))


if __name__ == "__main__":
    unittest.main(verbosity=2)
