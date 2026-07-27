from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from core import operations


class OperationQueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_root = os.environ.get("AGENELF_ROOT")
        os.environ["AGENELF_ROOT"] = str(self.root)

    def tearDown(self):
        if self.old_root is None:
            os.environ.pop("AGENELF_ROOT", None)
        else:
            os.environ["AGENELF_ROOT"] = self.old_root
        self.tmp.cleanup()

    def test_submit_has_stable_bound_fingerprint(self):
        request = operations.submit_operation(
            "server.operations",
            "apt_update",
            "primary",
            {},
            operations.RISK_CHANGE,
            "更新 APT",
        )
        payload = operations.canonical_payload(
            "server.operations", "apt_update", "primary", {}
        )
        self.assertEqual(request["fingerprint"], operations.payload_fingerprint(payload))
        path = self.root / "data" / "ops-requests" / f"{request['id']}.json"
        self.assertTrue(path.is_file())
        self.assertEqual(operations.get_operation(request["id"])["status"], "awaiting_approval")

    def test_read_request_is_queued_then_trusted_result_wins(self):
        request = operations.submit_operation(
            "server.operations",
            "inspect",
            "primary",
            {},
            operations.RISK_READ,
            "巡检",
        )
        self.assertEqual(operations.get_operation(request["id"])["status"], "queued")
        result_path = self.root / "data" / "ops-results" / f"{request['id']}.json"
        result_path.parent.mkdir(parents=True)
        result_path.write_text(
            json.dumps({"id": request["id"], "status": "succeeded"}),
            encoding="utf-8",
        )
        state = operations.get_operation(request["id"])
        self.assertEqual(state["status"], "succeeded")
        self.assertIn("result", state)

    def test_forbidden_never_creates_request(self):
        with self.assertRaises(PermissionError):
            operations.submit_operation(
                "server.operations",
                "raw_shell",
                "primary",
                {"command": "rm -rf /"},
                operations.RISK_FORBIDDEN,
                "禁止",
            )
        self.assertFalse((self.root / "data" / "ops-requests").exists())

    def test_invalid_operation_id_rejected(self):
        with self.assertRaises(ValueError):
            operations.get_operation("../../etc/passwd")


if __name__ == "__main__":
    unittest.main(verbosity=2)
