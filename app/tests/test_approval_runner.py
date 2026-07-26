from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from core import owner_approval

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT_ROOT / "scripts" / "approval_runner.py"
SPEC = importlib.util.spec_from_file_location("approval_runner_under_test", SCRIPT)
approval_runner = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(approval_runner)


class ApprovalRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for name in (
            "ops-requests",
            "auth-requests",
            "auth-decisions",
            "ops-results",
            "approval-commands",
            "approval-results",
            "approval-locks",
        ):
            (self.root / "data" / name).mkdir(parents=True, exist_ok=True)
        self.key = b"k" * 48

    def tearDown(self):
        self.tmp.cleanup()

    def _request(self):
        binding = {
            "capability": "server.operations",
            "operation": "compose_deploy",
            "target": "pve-ubuntu",
            "parameters": {"project": "vpn", "compose_yaml": "services: {}"},
        }
        request = {
            "schema_version": 1,
            "id": "op-0123456789abcdef",
            **binding,
            "risk": "change",
            "summary": "map 10808:1080",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "fingerprint": owner_approval.binding_fingerprint(binding),
        }
        (self.root / "data/ops-requests/op-0123456789abcdef.json").write_text(
            json.dumps(request), encoding="utf-8"
        )
        return request

    def test_runner_applies_signed_command_without_ssh_or_network(self):
        request = self._request()
        command = owner_approval.submit_owner_command(
            request["id"], root=self.root, key=self.key, decided_by="cli:windows"
        )
        runner = approval_runner.ApprovalRunner(root=self.root, key=self.key)
        counts = runner.run_once()
        self.assertEqual(counts.get("succeeded"), 1)
        result = owner_approval.wait_for_command_result(
            command["id"], root=self.root, timeout_seconds=0
        )
        self.assertEqual(result["status"], "succeeded")
        decision = json.loads(
            (
                self.root
                / "data/auth-decisions/op-0123456789abcdef.json"
            ).read_text()
        )
        self.assertEqual(decision["decision"], "approve")
        self.assertEqual(decision["fingerprint"], request["fingerprint"])

    def test_runner_records_invalid_signature_as_failure(self):
        request = self._request()
        command = owner_approval.submit_owner_command(
            request["id"], root=self.root, key=self.key
        )
        path = self.root / "data/approval-commands" / f"{command['id']}.json"
        document = json.loads(path.read_text())
        document["signature"] = "0" * 64
        path.write_text(json.dumps(document), encoding="utf-8")
        runner = approval_runner.ApprovalRunner(root=self.root, key=self.key)
        self.assertEqual(runner.run_once().get("failed"), 1)
        result = owner_approval.wait_for_command_result(
            command["id"], root=self.root, timeout_seconds=0
        )
        self.assertEqual(result["status"], "failed")
        self.assertIn("签名", result["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
