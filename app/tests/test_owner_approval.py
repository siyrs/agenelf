from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import owner_approval as oa


class OwnerApprovalTest(unittest.TestCase):
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
        self.key = b"x" * 48

    def tearDown(self):
        self.tmp.cleanup()

    def _request(self, rid="op-0123456789abcdef", port=10808):
        binding = {
            "capability": "server.operations",
            "operation": "compose_deploy",
            "target": "pve-ubuntu",
            "parameters": {"project": "vpn", "port": port},
        }
        request = {
            "schema_version": 1,
            "id": rid,
            **binding,
            "risk": "change",
            "summary": f"map {port}:1080",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "fingerprint": oa.binding_fingerprint(binding),
        }
        path = self.root / "data" / "ops-requests" / f"{rid}.json"
        path.write_text(json.dumps(request), encoding="utf-8")
        return request

    def test_direct_decision_is_exact_and_idempotent(self):
        request = self._request()
        result = oa.apply_owner_decision(
            request["id"], root=self.root, decided_by="cli:sirius"
        )
        self.assertEqual(result["decision"], "approve")
        again = oa.apply_owner_decision(
            request["id"], root=self.root, decided_by="cli:sirius"
        )
        self.assertTrue(again["idempotent"])
        decision = json.loads(
            (self.root / "data/auth-decisions" / f"{request['id']}.json").read_text()
        )
        self.assertEqual(decision["fingerprint"], request["fingerprint"])

    def test_tampered_request_is_rejected(self):
        request = self._request()
        path = self.root / "data/ops-requests" / f"{request['id']}.json"
        request["parameters"]["port"] = 9999
        path.write_text(json.dumps(request), encoding="utf-8")
        with self.assertRaisesRegex(oa.ApprovalError, "指纹"):
            oa.apply_owner_decision(request["id"], root=self.root)

    def test_signed_cli_command_round_trip(self):
        request = self._request()
        command = oa.submit_owner_command(
            request["id"], root=self.root, key=self.key, decided_by="cli:windows"
        )
        document = json.loads(
            (
                self.root
                / "data/approval-commands"
                / f"{command['id']}.json"
            ).read_text()
        )
        result = oa.process_owner_command(document, root=self.root, key=self.key)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["decision"]["decision"], "approve")

    def test_bad_signature_and_expired_command_are_rejected(self):
        request = self._request()
        command = oa.submit_owner_command(request["id"], root=self.root, key=self.key)
        path = self.root / "data/approval-commands" / f"{command['id']}.json"
        document = json.loads(path.read_text())
        document["reason"] = "tampered"
        with self.assertRaisesRegex(oa.ApprovalError, "签名"):
            oa.verify_owner_command(document, root=self.root, key=self.key)

        document = json.loads(path.read_text())
        future = datetime.now(timezone.utc) + timedelta(minutes=10)
        with self.assertRaisesRegex(oa.ApprovalError, "过期"):
            oa.verify_owner_command(
                document, root=self.root, key=self.key, at=future
            )

    def test_implicit_approval_selects_latest_duplicate_and_supersedes_old(self):
        first = self._request("op-1111111111111111")
        second = self._request("op-2222222222222222")
        p1 = self.root / "data/ops-requests" / f"{first['id']}.json"
        p2 = self.root / "data/ops-requests" / f"{second['id']}.json"
        first["created_at"] = "2026-01-01T00:00:00+00:00"
        second["created_at"] = "2026-01-02T00:00:00+00:00"
        p1.write_text(json.dumps(first), encoding="utf-8")
        p2.write_text(json.dumps(second), encoding="utf-8")
        selected, duplicates = oa.resolve_pending_operation(root=self.root)
        self.assertEqual(selected["id"], second["id"])
        self.assertEqual(duplicates, [first["id"]])
        command = oa.submit_owner_command(second["id"], root=self.root, key=self.key)
        document = json.loads(
            (
                self.root
                / "data/approval-commands"
                / f"{command['id']}.json"
            ).read_text()
        )
        result = oa.process_owner_command(document, root=self.root, key=self.key)
        self.assertEqual(
            result["decision"]["superseded_duplicates"], [first["id"]]
        )
        denied = json.loads(
            (self.root / "data/auth-decisions" / f"{first['id']}.json").read_text()
        )
        self.assertEqual(denied["decision"], "deny")

    def test_different_pending_payloads_require_exact_id(self):
        self._request("op-1111111111111111", port=10808)
        self._request("op-2222222222222222", port=18080)
        with self.assertRaises(oa.AmbiguousApprovalError) as caught:
            oa.resolve_pending_operation(root=self.root)
        self.assertEqual(len(caught.exception.pending), 2)

    def test_explicit_text_parser_is_conservative(self):
        self.assertEqual(
            oa.parse_owner_decision("审批通过"),
            {"action": "approve", "request_id": "", "reason": ""},
        )
        parsed = oa.parse_owner_decision(
            "/approve op-0123456789abcdef 端口改为10808"
        )
        self.assertEqual(parsed["action"], "approve")
        self.assertEqual(parsed["request_id"], "op-0123456789abcdef")
        self.assertIn("10808", parsed["reason"])
        self.assertIsNone(oa.parse_owner_decision("我觉得可以批准这个方案"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
