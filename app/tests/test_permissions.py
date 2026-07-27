from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from core import permissions


class PermissionsTest(unittest.TestCase):
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

    def _decision(self, request_id: str, decision: str = "approve", fingerprint: str | None = None):
        request = json.loads(
            (self.root / "data" / "auth-requests" / f"{request_id}.json").read_text(
                encoding="utf-8"
            )
        )
        path = self.root / "data" / "auth-decisions" / f"{request_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now().astimezone()
        path.write_text(
            json.dumps(
                {
                    "request_id": request_id,
                    "decision": decision,
                    "fingerprint": fingerprint or request["fingerprint"],
                    "expires_at": (now + timedelta(minutes=5)).isoformat(timespec="seconds"),
                }
            ),
            encoding="utf-8",
        )

    def test_command_classification(self):
        self.assertEqual(permissions.classify_command("uname -a"), "whitelist")
        self.assertEqual(permissions.classify_command("mkdir x"), "normal")
        self.assertEqual(permissions.classify_command("rm -rf /tmp/x"), "dangerous")
        self.assertEqual(permissions.classify_command("ufw disable"), "dangerous")

    def test_approval_is_bound_and_consumed_once(self):
        binding = {"target": "primary", "operation": "apt_update", "parameters": {}}
        ok, request_id = permissions.request_auth(
            "server_ops", "apt_update", "primary", binding=binding
        )
        self.assertTrue(ok)
        self.assertEqual(permissions.check_auth(request_id), permissions.STATUS_PENDING)
        self._decision(request_id)
        self.assertEqual(
            permissions.check_auth(request_id, expected_binding=binding),
            permissions.STATUS_APPROVED,
        )
        changed = {"target": "secondary", "operation": "apt_update", "parameters": {}}
        self.assertEqual(
            permissions.check_auth(request_id, expected_binding=changed),
            permissions.STATUS_BINDING_MISMATCH,
        )
        self.assertTrue(permissions.consume_auth(request_id, expected_binding=binding))
        self.assertFalse(permissions.consume_auth(request_id, expected_binding=binding))
        self.assertEqual(permissions.check_auth(request_id), permissions.STATUS_USED)

    def test_forged_decision_fingerprint_rejected(self):
        ok, request_id = permissions.request_auth(
            "server_ops", "restart", "nginx", binding={"service": "nginx"}
        )
        self.assertTrue(ok)
        self._decision(request_id, fingerprint="0" * 64)
        self.assertEqual(
            permissions.check_auth(request_id), permissions.STATUS_BINDING_MISMATCH
        )

    def test_denied_and_expired(self):
        ok, denied_id = permissions.request_auth("x", "y", "z")
        self.assertTrue(ok)
        self._decision(denied_id, decision="deny")
        self.assertEqual(permissions.check_auth(denied_id), permissions.STATUS_DENIED)

        ok, expired_id = permissions.request_auth("x", "y", "z")
        self.assertTrue(ok)
        request_path = self.root / "data" / "auth-requests" / f"{expired_id}.json"
        request = json.loads(request_path.read_text(encoding="utf-8"))
        request["expires_at"] = (
            datetime.now().astimezone() - timedelta(seconds=1)
        ).isoformat(timespec="seconds")
        request_path.write_text(json.dumps(request), encoding="utf-8")
        self.assertEqual(permissions.check_auth(expired_id), permissions.STATUS_EXPIRED)

    def test_pending_limit(self):
        for _ in range(permissions.MAX_PENDING_REQUESTS):
            ok, _request_id = permissions.request_auth("x", "y", "z")
            self.assertTrue(ok)
        ok, message = permissions.request_auth("x", "y", "z")
        self.assertFalse(ok)
        self.assertIn("上限", message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
