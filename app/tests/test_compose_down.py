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
from core.execution_policy import resolve_contract
from skills import compose_lifecycle

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "agenelf_compose_down_runner", SCRIPTS / "unified_ops_runner.py"
)
unified = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = unified
SPEC.loader.exec_module(unified)


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
        return unified.CommandResult(command, 0, "ok\n", "")


class ComposeDownTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.servers = self.root / "servers.yaml"
        self.servers.write_text(
            """servers:
  primary:
    host: 127.0.0.1
    username: agenelf
    managed_root: /srv/agenelf
    docker_command: docker
    # Backward-compatible deploy permission must still require exact approval.
    allowed_operations: [compose_deploy]
""",
            encoding="utf-8",
        )
        self.old_root = os.environ.get("AGENELF_ROOT")
        self.old_servers = os.environ.get("AGENELF_SERVERS_FILE")
        os.environ["AGENELF_ROOT"] = str(self.root)
        os.environ["AGENELF_SERVERS_FILE"] = str(self.servers)
        FakeSession.commands = []
        self.runner = unified.UnifiedOpsRunner(
            root=self.root,
            servers_file=self.servers,
            secrets_root=self.root / "secrets",
            session_factory=FakeSession,
        )

    def tearDown(self):
        if self.old_root is None:
            os.environ.pop("AGENELF_ROOT", None)
        else:
            os.environ["AGENELF_ROOT"] = self.old_root
        if self.old_servers is None:
            os.environ.pop("AGENELF_SERVERS_FILE", None)
        else:
            os.environ["AGENELF_SERVERS_FILE"] = self.old_servers
        self.tmp.cleanup()

    def _request(self) -> dict:
        paths = sorted((self.root / "data" / "ops-requests").glob("op-*.json"))
        self.assertEqual(len(paths), 1)
        return json.loads(paths[0].read_text(encoding="utf-8"))

    def _approve(self, request: dict) -> None:
        path = self.root / "data" / "auth-decisions" / f"{request['id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "request_id": request["id"],
                    "decision": "approve",
                    "fingerprint": request["fingerprint"],
                    "expires_at": (
                        datetime.now().astimezone() + timedelta(minutes=5)
                    ).isoformat(timespec="seconds"),
                }
            ),
            encoding="utf-8",
        )

    def test_plan_is_non_destructive_and_does_not_submit(self):
        result = compose_lifecycle.down_compose_project(
            "primary", "vpn", timeout_seconds=45, plan_only=True
        )
        self.assertIn("计划校验通过", result)
        self.assertIn('"named_volumes"', result)
        self.assertIn('"--volumes"', result)
        self.assertFalse((self.root / "data" / "ops-requests").exists())

    def test_change_creates_exact_request_with_cross_platform_approval_hint(self):
        result = compose_lifecycle.down_compose_project(
            "primary", "vpn", timeout_seconds=45, remove_orphans=True
        )
        self.assertIn("Compose down 请求已创建：op-", result)
        self.assertIn("/approve op-", result)
        self.assertIn("approve.ps1", result)
        request = self._request()
        self.assertEqual(request["capability"], "server.operations")
        self.assertEqual(request["operation"], "compose_down")
        self.assertEqual(request["risk"], operations.RISK_CHANGE)
        self.assertEqual(
            request["parameters"],
            {"project": "vpn", "timeout_seconds": 45, "remove_orphans": True},
        )

    def test_runner_waits_for_approval_and_never_deletes_volumes_or_images(self):
        compose_lifecycle.down_compose_project(
            "primary", "vpn", timeout_seconds=30, remove_orphans=True
        )
        request = self._request()
        self.assertEqual(self.runner.run_once().get("pending"), 1)
        self.assertFalse(FakeSession.commands)
        self._approve(request)
        self.assertEqual(self.runner.run_once().get("succeeded"), 1)
        joined = "\n".join(FakeSession.commands)
        self.assertIn("/srv/agenelf/vpn/compose.yaml", joined)
        self.assertIn("compose -f", joined)
        self.assertIn("down --timeout 30 --remove-orphans", joined)
        self.assertIn("compose -f", joined)
        self.assertIn("ps -a", joined)
        self.assertNotIn("--volumes", joined)
        self.assertNotIn("--rmi", joined)
        state = operations.get_operation(request["id"], root=self.root)
        self.assertEqual(state["status"], "succeeded")
        self.assertIn("named_volumes", state["result"]["preserved"])

    def test_invalid_project_or_policy_is_rejected_before_request(self):
        self.assertIn(
            "project 只能包含",
            compose_lifecycle.down_compose_project("primary", "../vpn"),
        )
        self.servers.write_text(
            """servers:
  primary:
    host: 127.0.0.1
    username: agenelf
    allowed_operations: [inspect]
""",
            encoding="utf-8",
        )
        self.assertIn(
            "未允许 compose_down",
            compose_lifecycle.down_compose_project("primary", "vpn"),
        )

    def test_registry_contract_is_change_through_queued_runner(self):
        contract = resolve_contract("down_compose_project", {}, compose_lifecycle)
        self.assertIsNotNone(contract)
        self.assertEqual(contract.capability, "server.operations")
        self.assertEqual(contract.operation, "compose_down")
        self.assertEqual(contract.risk, "change")
        self.assertEqual(contract.execution_mode, "queued_runner")


if __name__ == "__main__":
    unittest.main(verbosity=2)
