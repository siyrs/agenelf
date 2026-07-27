from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import approval_catalog, operations, owner_approval


class ApprovalCatalogOperationExpiryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_root = os.environ.get("AGENELF_ROOT")
        os.environ["AGENELF_ROOT"] = str(self.root)

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

    def test_expired_operation_is_absent_from_pending_catalog(self) -> None:
        expired = operations.submit_operation(
            "server.operations",
            "apt_update",
            "primary",
            {},
            operations.RISK_CHANGE,
            "expired",
            root=self.root,
        )
        self._expire(expired["id"])
        live = operations.submit_operation(
            "server.operations",
            "service_restart",
            "primary",
            {"service": "nginx"},
            operations.RISK_CHANGE,
            "live",
            root=self.root,
        )

        pending = approval_catalog.list_pending_requests(self.root)

        self.assertEqual([item["id"] for item in pending], [live["id"]])
        with self.assertRaisesRegex(owner_approval.ApprovalError, "已过期"):
            approval_catalog.resolve_pending_request(expired["id"], self.root)

    def test_implicit_approval_chooses_live_request_not_expired_duplicate(self) -> None:
        old = operations.submit_operation(
            "server.operations",
            "apt_update",
            "primary",
            {},
            operations.RISK_CHANGE,
            "old",
            root=self.root,
            deduplicate=False,
        )
        self._expire(old["id"])
        live = operations.submit_operation(
            "server.operations",
            "apt_update",
            "primary",
            {},
            operations.RISK_CHANGE,
            "live",
            root=self.root,
            deduplicate=False,
        )

        selected, duplicates = approval_catalog.resolve_pending_request(root=self.root)

        self.assertEqual(selected["id"], live["id"])
        self.assertEqual(duplicates, [])

    def test_expired_authorization_request_is_rejected_explicitly(self) -> None:
        request_id = "auth-0123456789ab"
        path = self.root / "data" / "auth-requests" / f"{request_id}.json"
        path.parent.mkdir(parents=True)
        binding = {"kind": "example", "session_id": "upgrade-example"}
        path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "id": request_id,
                    "binding": binding,
                    "fingerprint": owner_approval.binding_fingerprint(binding),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "expires_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(owner_approval.ApprovalError, "已过期"):
            approval_catalog.resolve_pending_request(request_id, self.root)


if __name__ == "__main__":
    unittest.main(verbosity=2)
