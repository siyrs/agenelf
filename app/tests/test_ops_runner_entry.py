from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import operations

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "agenelf_ops_runner_entry",
    ROOT / "scripts" / "ops_runner_entry.py",
)
entry = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = entry
SPEC.loader.exec_module(entry)


class FakeSession:
    commands: list[str] = []

    def __init__(self, profile, secrets_root):
        self.profile = profile
        self.secrets_root = secrets_root

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def run(self, command: str, timeout: int = 120):
        self.__class__.commands.append(command)
        return entry.CommandResult(command, 0, "ok\n", "")

    def write_text(self, remote_path: str, content: str):
        raise AssertionError("not expected")


class OpsRunnerEntryTest(unittest.TestCase):
    def setUp(self) -> None:
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
    docker_command: docker
    allowed_operations: [inspect, apt_update]
""",
            encoding="utf-8",
        )
        FakeSession.commands = []
        self.runner = entry.LifecycleOpsRunner(
            root=self.root,
            servers_file=self.servers_file,
            secrets_root=self.root / "secrets",
            session_factory=FakeSession,
        )

    def tearDown(self) -> None:
        if self.old_root is None:
            os.environ.pop("AGENELF_ROOT", None)
        else:
            os.environ["AGENELF_ROOT"] = self.old_root
        self.tmp.cleanup()

    def _expire(self, request_id: str) -> None:
        path = self.root / "data" / "ops-requests" / f"{request_id}.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_expired_request_is_finalized_without_ssh(self) -> None:
        request = operations.submit_operation(
            "server.operations",
            "inspect",
            "primary",
            {},
            operations.RISK_READ,
            "inspect",
            root=self.root,
        )
        self._expire(request["id"])

        counts = self.runner.run_once()

        self.assertEqual(counts.get("expired"), 1)
        self.assertEqual(FakeSession.commands, [])
        state = operations.get_operation(request["id"], root=self.root)
        self.assertEqual(state["status"], "expired")
        self.assertEqual(state["result"]["commands"], [])
        self.assertIn("未连接服务器", state["result"]["reason"])

    def test_live_request_still_uses_full_unified_runner_validation(self) -> None:
        request = operations.submit_operation(
            "server.operations",
            "inspect",
            "primary",
            {},
            operations.RISK_READ,
            "inspect",
            root=self.root,
        )

        counts = self.runner.run_once()

        self.assertEqual(counts.get("succeeded"), 1)
        self.assertTrue(FakeSession.commands)
        self.assertEqual(
            operations.get_operation(request["id"], root=self.root)["status"],
            "succeeded",
        )

    def test_request_without_any_valid_lifetime_fails_closed(self) -> None:
        request_id = "op-0123456789abcdef"
        request_path = self.root / "data" / "ops-requests" / f"{request_id}.json"
        request_path.parent.mkdir(parents=True)
        payload = operations.canonical_payload("server.operations", "inspect", "primary", {})
        request_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "id": request_id,
                    **payload,
                    "risk": "read",
                    "fingerprint": operations.payload_fingerprint(payload),
                    "created_at": "not-a-time",
                }
            ),
            encoding="utf-8",
        )

        counts = self.runner.run_once()

        self.assertEqual(counts.get("expired"), 1)
        self.assertFalse(FakeSession.commands)


if __name__ == "__main__":
    unittest.main(verbosity=2)
