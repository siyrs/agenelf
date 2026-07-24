from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.capability_health import CapabilityHealth


class CapabilityHealthTest(unittest.TestCase):
    @staticmethod
    def _write(path: Path, value: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_scorecards_are_derived_from_trusted_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index, status in enumerate(("failed", "failed", "succeeded")):
                operation_id = f"op-{index:016x}"
                self._write(
                    root / "data" / "ops-requests" / f"{operation_id}.json",
                    {
                        "id": operation_id,
                        "capability": "server.operations",
                        "operation": "inspect",
                        "target": "primary",
                        "summary": "巡检",
                        "created_at": f"2026-01-01T00:00:0{index}+00:00",
                    },
                )
                self._write(
                    root / "data" / "ops-results" / f"{operation_id}.json",
                    {
                        "id": operation_id,
                        "status": status,
                        "finished_at": f"2026-01-01T00:00:0{index}+00:00",
                        "reason": "boom" if status == "failed" else "",
                    },
                )
            for index in range(2):
                validation_id = f"val-{index:016x}"
                self._write(
                    root / "data" / "validation-requests" / f"{validation_id}.json",
                    {
                        "id": validation_id,
                        "operation": "run_check",
                        "target": "api-health",
                        "created_at": f"2026-01-02T00:00:0{index}+00:00",
                    },
                )
                self._write(
                    root / "data" / "validation-results" / f"{validation_id}.json",
                    {
                        "id": validation_id,
                        "status": "failed",
                        "summary": "0/1 failed",
                        "finished_at": f"2026-01-02T00:00:0{index}+00:00",
                    },
                )

            health = CapabilityHealth(root)
            snapshot = health.snapshot()
            server = snapshot["scorecards"]["server.operations"]
            validation = snapshot["scorecards"]["software.validation"]
            self.assertEqual(server["health"], "degraded")
            self.assertEqual(validation["health"], "degraded")
            self.assertEqual(validation["consecutive_failures"], 2)
            findings = health.findings()
            self.assertTrue(
                any(item["code"] == "capability_degraded:software.validation" for item in findings)
            )
            roadmap = health.roadmap(
                [
                    {
                        "id": "intent-1",
                        "title": "修复验证",
                        "priority": "P1",
                        "status": "proposed",
                        "evidence": ["validation:1"],
                        "attempts": 0,
                        "owner_aligned": True,
                    },
                    {
                        "id": "intent-2",
                        "title": "低优先级",
                        "priority": "P3",
                        "status": "proposed",
                        "evidence": [],
                        "attempts": 0,
                        "owner_aligned": False,
                    },
                ]
            )
            self.assertEqual(roadmap["recommended"]["id"], "intent-1")
            serialized = json.dumps(snapshot, ensure_ascii=False)
            self.assertNotIn("password", serialized.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
