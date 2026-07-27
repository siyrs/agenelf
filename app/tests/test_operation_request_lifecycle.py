from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import operations


class OperationRequestLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_root = os.environ.get("AGENELF_ROOT")
        self.old_env = {
            name: os.environ.get(name)
            for name in (
                "AGENELF_OPERATION_READ_TTL_SECONDS",
                "AGENELF_OPERATION_CHANGE_TTL_SECONDS",
                "AGENELF_OPERATION_PRIVILEGED_TTL_SECONDS",
            )
        }
        os.environ["AGENELF_ROOT"] = str(self.root)

    def tearDown(self) -> None:
        if self.old_root is None:
            os.environ.pop("AGENELF_ROOT", None)
        else:
            os.environ["AGENELF_ROOT"] = self.old_root
        for name, value in self.old_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.tmp.cleanup()

    def _request_path(self, request_id: str) -> Path:
        return self.root / "data" / "ops-requests" / f"{request_id}.json"

    def _decision_path(self, request_id: str) -> Path:
        return self.root / "data" / "auth-decisions" / f"{request_id}.json"

    def test_identical_unfinished_change_request_is_reused(self) -> None:
        first = operations.submit_operation(
            "server.operations",
            "apt_update",
            "primary",
            {},
            operations.RISK_CHANGE,
            "first summary",
        )
        second = operations.submit_operation(
            "server.operations",
            "apt_update",
            "primary",
            {},
            operations.RISK_CHANGE,
            "second summary",
        )

        self.assertEqual(second["id"], first["id"])
        self.assertTrue(second["reused_existing"])
        self.assertEqual(second["reuse_reason"], "identical_unfinished_request")
        self.assertEqual(len(list((self.root / "data" / "ops-requests").glob("op-*.json"))), 1)

    def test_different_payload_or_explicit_no_dedupe_creates_new_request(self) -> None:
        first = operations.submit_operation(
            "server.operations",
            "service_restart",
            "primary",
            {"service": "nginx"},
            operations.RISK_CHANGE,
            "restart nginx",
        )
        second = operations.submit_operation(
            "server.operations",
            "service_restart",
            "primary",
            {"service": "docker"},
            operations.RISK_CHANGE,
            "restart docker",
        )
        third = operations.submit_operation(
            "server.operations",
            "service_restart",
            "primary",
            {"service": "nginx"},
            operations.RISK_CHANGE,
            "parallel explicit request",
            deduplicate=False,
        )

        self.assertNotEqual(first["id"], second["id"])
        self.assertNotEqual(first["id"], third["id"])

    def test_finished_read_request_is_not_reused_because_fresh_data_is_required(self) -> None:
        first = operations.submit_operation(
            "server.operations",
            "inspect",
            "primary",
            {},
            operations.RISK_READ,
            "inspect",
        )
        result_path = self.root / "data" / "ops-results" / f"{first['id']}.json"
        result_path.parent.mkdir(parents=True)
        result_path.write_text(
            json.dumps({"id": first["id"], "status": "succeeded"}),
            encoding="utf-8",
        )

        second = operations.submit_operation(
            "server.operations",
            "inspect",
            "primary",
            {},
            operations.RISK_READ,
            "inspect again",
        )
        self.assertNotEqual(second["id"], first["id"])

    def test_expired_request_is_terminal_and_is_not_reused(self) -> None:
        first = operations.submit_operation(
            "server.operations",
            "apt_update",
            "primary",
            {},
            operations.RISK_CHANGE,
            "apt",
        )
        path = self._request_path(first["id"])
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        path.write_text(json.dumps(stored), encoding="utf-8")

        state = operations.get_operation(first["id"])
        self.assertEqual(state["status"], "expired")
        self.assertIn("重新提交", state["next_action"])

        second = operations.submit_operation(
            "server.operations",
            "apt_update",
            "primary",
            {},
            operations.RISK_CHANGE,
            "apt retry",
        )
        self.assertNotEqual(second["id"], first["id"])

    def test_expired_approval_is_not_reused(self) -> None:
        first = operations.submit_operation(
            "server.operations",
            "apt_update",
            "primary",
            {},
            operations.RISK_CHANGE,
            "apt",
        )
        decision = self._decision_path(first["id"])
        decision.parent.mkdir(parents=True)
        decision.write_text(
            json.dumps(
                {
                    "request_id": first["id"],
                    "decision": "approve",
                    "fingerprint": first["fingerprint"],
                    "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                }
            ),
            encoding="utf-8",
        )

        self.assertEqual(operations.get_operation(first["id"])["status"], "approval_expired")
        second = operations.submit_operation(
            "server.operations",
            "apt_update",
            "primary",
            {},
            operations.RISK_CHANGE,
            "apt retry",
        )
        self.assertNotEqual(second["id"], first["id"])

    def test_legacy_request_without_explicit_expiry_uses_created_at_and_risk_ttl(self) -> None:
        os.environ["AGENELF_OPERATION_CHANGE_TTL_SECONDS"] = "30"
        request = operations.submit_operation(
            "server.operations",
            "apt_update",
            "primary",
            {},
            operations.RISK_CHANGE,
            "legacy",
        )
        path = self._request_path(request["id"])
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored.pop("expires_at")
        stored.pop("ttl_seconds")
        stored["created_at"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        path.write_text(json.dumps(stored), encoding="utf-8")

        self.assertTrue(operations.request_expired(stored, fail_closed=True))
        self.assertEqual(operations.get_operation(request["id"])["status"], "expired")

    def test_ttl_is_bounded_and_written_to_request(self) -> None:
        request = operations.submit_operation(
            "server.operations",
            "inspect",
            "primary",
            {},
            operations.RISK_READ,
            "inspect",
            ttl_seconds=1,
        )
        self.assertEqual(request["ttl_seconds"], 15)
        self.assertIsNotNone(operations.request_expiry(request))


if __name__ == "__main__":
    unittest.main(verbosity=2)
