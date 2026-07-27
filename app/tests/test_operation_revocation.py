from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import operation_revocation, operations


class OperationRevocationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _request(self, *, ttl_seconds: int = 1800) -> dict:
        return operations.submit_operation(
            "server.operations",
            "apt_update",
            "primary",
            {},
            operations.RISK_CHANGE,
            "更新 APT",
            root=self.root,
            ttl_seconds=ttl_seconds,
            deduplicate=False,
        )

    def _approve(self, request: dict) -> None:
        path = self.root / "data" / "auth-decisions" / f"{request['id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "request_id": request["id"],
                    "decision": "approve",
                    "fingerprint": request["fingerprint"],
                    "decided_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "expires_at": (
                        datetime.now(timezone.utc) + timedelta(minutes=5)
                    ).isoformat(timespec="seconds"),
                }
            ),
            encoding="utf-8",
        )

    def test_pending_request_is_cancelled_without_commands(self) -> None:
        request = self._request()
        result = operation_revocation.revoke_operation(
            request["id"],
            "主人改变了任务范围",
            "host:sirius",
            root=self.root,
        )

        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(result["started"])
        trusted = operations.get_operation(request["id"], root=self.root)
        self.assertEqual(trusted["status"], "cancelled")
        self.assertEqual(trusted["result"]["commands"], [])
        self.assertEqual(
            trusted["result"]["cancellation"]["request_fingerprint"],
            request["fingerprint"],
        )
        self.assertTrue(
            (self.root / "data" / "ops-requests" / f"{request['id']}.json").is_file()
        )
        self.assertFalse(
            (self.root / "data" / "ops-locks" / f"{request['id']}.lock").exists()
        )

    def test_approved_but_not_started_request_can_be_revoked(self) -> None:
        request = self._request()
        self._approve(request)

        result = operation_revocation.revoke_operation(
            request["id"], root=self.root, cancelled_by="host:sirius"
        )

        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(
            operations.get_operation(request["id"], root=self.root)["status"],
            "cancelled",
        )

    def test_active_runner_lock_fails_closed(self) -> None:
        request = self._request()
        lock = self.root / "data" / "ops-locks" / f"{request['id']}.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("", encoding="utf-8")

        with self.assertRaisesRegex(
            operation_revocation.OperationRevocationError,
            "已经开始|执行锁",
        ):
            operation_revocation.revoke_operation(request["id"], root=self.root)

        self.assertFalse(
            (self.root / "data" / "ops-results" / f"{request['id']}.json").exists()
        )

    def test_existing_terminal_result_cannot_be_overwritten(self) -> None:
        request = self._request()
        path = self.root / "data" / "ops-results" / f"{request['id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"id": request["id"], "status": "succeeded"}),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            operation_revocation.OperationRevocationError,
            "已有可信终态",
        ):
            operation_revocation.revoke_operation(request["id"], root=self.root)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"], "succeeded")

    def test_cancel_is_idempotent_after_success(self) -> None:
        request = self._request()
        first = operation_revocation.revoke_operation(request["id"], root=self.root)
        second = operation_revocation.revoke_operation(request["id"], root=self.root)

        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(second["status"], "cancelled")

    def test_expired_request_is_not_reactivated_by_revocation(self) -> None:
        request = self._request(ttl_seconds=15)
        request_path = self.root / "data" / "ops-requests" / f"{request['id']}.json"
        value = json.loads(request_path.read_text(encoding="utf-8"))
        value["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(
            timespec="seconds"
        )
        request_path.write_text(json.dumps(value), encoding="utf-8")

        with self.assertRaisesRegex(
            operation_revocation.OperationRevocationError,
            "已过期",
        ):
            operation_revocation.revoke_operation(request["id"], root=self.root)

    def test_tampered_request_is_rejected(self) -> None:
        request = self._request()
        request_path = self.root / "data" / "ops-requests" / f"{request['id']}.json"
        value = json.loads(request_path.read_text(encoding="utf-8"))
        value["target"] = "other"
        request_path.write_text(json.dumps(value), encoding="utf-8")

        with self.assertRaisesRegex(
            operation_revocation.OperationRevocationError,
            "指纹不匹配",
        ):
            operation_revocation.revoke_operation(request["id"], root=self.root)

    def test_read_only_status_does_not_expose_parameters(self) -> None:
        request = operations.submit_operation(
            "server.operations",
            "compose_deploy",
            "primary",
            {"project": "vpn", "compose_yaml": "token=SHOULD-NOT-LEAK"},
            operations.RISK_CHANGE,
            "部署 VPN",
            root=self.root,
            deduplicate=False,
        )

        rows = operation_revocation.list_revocable_operations(root=self.root)
        self.assertEqual([item["id"] for item in rows], [request["id"]])
        serialized = json.dumps(rows, ensure_ascii=False)
        self.assertNotIn("parameters", serialized)
        self.assertNotIn("SHOULD-NOT-LEAK", serialized)
        self.assertTrue(rows[0]["revocable"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
