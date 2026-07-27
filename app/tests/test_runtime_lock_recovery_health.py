from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core import runtime_health


class RuntimeLockRecoveryHealthTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
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

    def _heartbeat(
        self,
        name: str,
        *,
        reclaimed: int = 0,
        skipped: int = 0,
    ) -> None:
        value = {
            "schema_version": 2,
            "runner": name,
            "status": "running",
            "heartbeat_at": (self.now - timedelta(seconds=1)).isoformat(),
            "expires_after_seconds": 6,
            "sequence": 5,
            "runtime_source": "app-bind",
            "lock_recovery": {
                "lock_dir": f"data/{name}-locks",
                "reclaimed": reclaimed,
                "skipped": skipped,
            },
            "private_key": "MUST-NOT-LEAK",
        }
        (self.root / "data" / "runner-health" / f"{name}.json").write_text(
            json.dumps(value),
            encoding="utf-8",
        )

    def test_doctor_reports_reclaimed_locks_without_exposing_unknown_fields(self) -> None:
        for name in runtime_health.DEFAULT_RUNNERS:
            self._heartbeat(name, reclaimed=2 if name == "ops-runner" else 0)

        result = runtime_health.diagnose(
            self.root,
            config={"runtime_health": {"stale_after_seconds": 10}},
            at=self.now,
        )

        self.assertEqual(result["status"], "healthy")
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["runners"]["reclaimed_locks"], 2)
        self.assertEqual(
            result["runners"]["runners"]["ops-runner"]["lock_recovery"]["reclaimed"],
            2,
        )
        combined = "\n".join(result["recommendations"])
        self.assertIn("自动回收 2 个", combined)
        self.assertNotIn("MUST-NOT-LEAK", json.dumps(result, ensure_ascii=False))

    def test_non_regular_lock_entry_keeps_doctor_degraded(self) -> None:
        for name in runtime_health.DEFAULT_RUNNERS:
            self._heartbeat(name, skipped=1 if name == "repair-runner" else 0)

        result = runtime_health.diagnose(
            self.root,
            config={"runtime_health": {"stale_after_seconds": 10}},
            at=self.now,
        )

        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["runners"]["skipped_lock_entries"], 1)
        self.assertIn("非普通文件", "\n".join(result["recommendations"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
