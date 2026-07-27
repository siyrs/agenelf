from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from core import operations, runtime_health


class RuntimeOperationRequestHealthTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = datetime.now(timezone.utc)
        self.old_root = os.environ.get("AGENELF_ROOT")
        self.old_source = os.environ.get("AGENELF_RUNTIME_SOURCE")
        os.environ["AGENELF_ROOT"] = str(self.root)
        os.environ["AGENELF_RUNTIME_SOURCE"] = "app-bind"
        for relative in (
            "app-fork",
            "app-tmp",
            "data/auth-decisions",
            "data/runner-health",
            "data/ops-requests",
            "data/ops-results",
            "data/authorized-upgrades",
            "data/continuations",
            "local",
        ):
            (self.root / relative).mkdir(parents=True, exist_ok=True)
        (self.root / "local" / "servers.yaml").write_text(
            "schema_version: 1\nservers: {}\n",
            encoding="utf-8",
        )
        for name in runtime_health.DEFAULT_RUNNERS:
            (self.root / "data" / "runner-health" / f"{name}.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "runner": name,
                        "status": "running",
                        "started_at": (self.now - timedelta(minutes=1)).isoformat(),
                        "heartbeat_at": self.now.isoformat(),
                        "sequence": 5,
                        "expires_after_seconds": 6,
                        "runtime_source": "app-bind",
                        "lock_recovery": {
                            "lock_dir": f"data/{name}-locks",
                            "reclaimed": 0,
                            "skipped": 0,
                        },
                    }
                ),
                encoding="utf-8",
            )

    def tearDown(self) -> None:
        if self.old_root is None:
            os.environ.pop("AGENELF_ROOT", None)
        else:
            os.environ["AGENELF_ROOT"] = self.old_root
        if self.old_source is None:
            os.environ.pop("AGENELF_RUNTIME_SOURCE", None)
        else:
            os.environ["AGENELF_RUNTIME_SOURCE"] = self.old_source
        self.tmp.cleanup()

    def _expire(self, request_id: str) -> None:
        path = self.root / "data" / "ops-requests" / f"{request_id}.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["expires_at"] = (self.now - timedelta(seconds=1)).isoformat()
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_doctor_distinguishes_live_duplicate_and_expired_operations(self) -> None:
        expired = operations.submit_operation(
            "server.operations",
            "apt_update",
            "primary",
            {},
            operations.RISK_CHANGE,
            "expired",
            root=self.root,
            deduplicate=False,
        )
        self._expire(expired["id"])
        operations.submit_operation(
            "server.operations",
            "service_restart",
            "primary",
            {"service": "nginx"},
            operations.RISK_CHANGE,
            "duplicate one",
            root=self.root,
            deduplicate=False,
        )
        operations.submit_operation(
            "server.operations",
            "service_restart",
            "primary",
            {"service": "nginx"},
            operations.RISK_CHANGE,
            "duplicate two",
            root=self.root,
            deduplicate=False,
        )

        result = runtime_health.diagnose(
            self.root,
            registry=SimpleNamespace(errors={}),
            config={"runtime_health": {"stale_after_seconds": 10}},
            at=self.now,
        )

        queues = result["queues"]
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(queues["expired_unresolved_operations"], 1)
        self.assertEqual(queues["pending_operations"], 2)
        self.assertEqual(queues["duplicate_pending_operations"], 1)
        self.assertEqual(queues["invalid_operation_requests"], 0)
        self.assertIn("过期请求 1", result["summary"])
        self.assertIn("重复待办 1", result["summary"])
        recommendations = "\n".join(result["recommendations"])
        self.assertIn("标记 expired", recommendations)
        self.assertIn("历史重复待办", recommendations)

    def test_completed_requests_are_not_counted_as_queue_debt(self) -> None:
        request = operations.submit_operation(
            "server.operations",
            "inspect",
            "primary",
            {},
            operations.RISK_READ,
            "done",
            root=self.root,
        )
        result_path = self.root / "data" / "ops-results" / f"{request['id']}.json"
        result_path.write_text(
            json.dumps({"id": request["id"], "status": "succeeded"}),
            encoding="utf-8",
        )

        result = runtime_health.diagnose(
            self.root,
            registry=SimpleNamespace(errors={}),
            config={"runtime_health": {"stale_after_seconds": 10}},
            at=self.now,
        )

        self.assertEqual(result["status"], "healthy")
        self.assertEqual(result["queues"]["pending_operations"], 0)
        self.assertEqual(result["queues"]["expired_unresolved_operations"], 0)
        self.assertEqual(result["queues"]["duplicate_pending_operations"], 0)

    def test_invalid_request_is_visible_and_degrades_doctor(self) -> None:
        path = self.root / "data" / "ops-requests" / "op-0123456789abcdef.json"
        path.write_text("{broken", encoding="utf-8")

        result = runtime_health.diagnose(
            self.root,
            registry=SimpleNamespace(errors={}),
            config={"runtime_health": {"stale_after_seconds": 10}},
            at=self.now,
        )

        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["queues"]["invalid_operation_requests"], 1)
        self.assertIn("无效运维请求", "\n".join(result["recommendations"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
