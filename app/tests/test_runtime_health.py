from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from core import runtime_health


class RuntimeHealthTest(unittest.TestCase):
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
            "schema_version: 1\nservers: {}\n", encoding="utf-8"
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
        age_seconds: float = 1.0,
        status: str = "running",
        extra: dict | None = None,
    ) -> None:
        value = {
            "schema_version": 1,
            "runner": name,
            "status": status,
            "started_at": (self.now - timedelta(minutes=1)).isoformat(),
            "heartbeat_at": (self.now - timedelta(seconds=age_seconds)).isoformat(),
            "sequence": 10,
            "expires_after_seconds": 6,
            "runtime_source": "app-bind",
        }
        value.update(extra or {})
        path = self.root / "data" / "runner-health" / f"{name}.json"
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_runner_health_distinguishes_healthy_stale_missing_and_invalid(self) -> None:
        self._heartbeat("healthy-runner", age_seconds=2)
        self._heartbeat("stale-runner", age_seconds=60)
        invalid = self.root / "data" / "runner-health" / "invalid-runner.json"
        invalid.write_text("{broken", encoding="utf-8")

        result = runtime_health.runner_health(
            self.root,
            expected=(
                "healthy-runner",
                "stale-runner",
                "missing-runner",
                "invalid-runner",
            ),
            stale_after_seconds=10,
            at=self.now,
        )

        self.assertEqual(result["healthy"], 1)
        self.assertEqual(result["unhealthy"], 3)
        self.assertEqual(result["runners"]["healthy-runner"]["health"], "healthy")
        self.assertEqual(result["runners"]["stale-runner"]["health"], "stale")
        self.assertEqual(result["runners"]["missing-runner"]["health"], "missing")
        self.assertEqual(result["runners"]["invalid-runner"]["health"], "invalid")

    def test_doctor_reports_healthy_runtime_without_exposing_unknown_heartbeat_fields(self) -> None:
        for name in runtime_health.DEFAULT_RUNNERS:
            self._heartbeat(
                name,
                extra={
                    "private_key": "SHOULD-NOT-LEAK",
                    "request_parameters": {"token": "SHOULD-NOT-LEAK"},
                },
            )
        (self.root / "data" / "continuations" / "pending.json").write_text(
            "{}", encoding="utf-8"
        )

        result = runtime_health.diagnose(
            self.root,
            registry=SimpleNamespace(errors={}),
            config={"runtime_health": {"stale_after_seconds": 10}},
            at=self.now,
        )

        self.assertEqual(result["status"], "healthy")
        self.assertTrue(result["runners"]["all_healthy"])
        self.assertTrue(result["queues"]["task_continuation_exists"])
        self.assertNotIn("SHOULD-NOT-LEAK", json.dumps(result, ensure_ascii=False))
        self.assertEqual(result["recommendations"], [])

    def test_doctor_returns_actionable_degraded_evidence(self) -> None:
        for name in runtime_health.DEFAULT_RUNNERS:
            self._heartbeat(name)
        self._heartbeat("ops-runner", age_seconds=120)
        (self.root / "data" / "ops-requests" / "op-0123456789abcdef.json").write_text(
            "{}", encoding="utf-8"
        )
        (self.root / "data" / "authorized-upgrades" / "upgrade-example.json").write_text(
            json.dumps({"id": "upgrade-example", "status": "failed"}),
            encoding="utf-8",
        )
        registry = SimpleNamespace(errors={"broken_skill": "Traceback\nRuntimeError: boom"})

        result = runtime_health.diagnose(
            self.root,
            registry=registry,
            config={"runtime_health": {"stale_after_seconds": 10}},
            at=self.now,
        )

        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["runners"]["runners"]["ops-runner"]["health"], "stale")
        self.assertEqual(result["queues"]["pending_operations"], 1)
        self.assertEqual(result["queues"]["failed_authorized_upgrades"], 1)
        self.assertEqual(result["registry_errors"]["broken_skill"], "RuntimeError: boom")
        combined = "\n".join(result["recommendations"])
        self.assertIn("ops-runner", combined)
        self.assertIn("/skills", combined)
        self.assertIn("/upgrade status", combined)


if __name__ == "__main__":
    unittest.main(verbosity=2)
