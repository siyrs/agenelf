from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from core.agent import Agent

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class AgentCapabilityHealthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.local = self.root / "local"
        for directory in (self.local / "context", self.local / "memory", self.local / "self"):
            directory.mkdir(parents=True)
        (self.local / "profile.yaml").write_text("owner: {name: Test}\n", encoding="utf-8")
        (self.local / "preferences.yaml").write_text("hobbies: [quality]\n", encoding="utf-8")
        (self.local / "servers.yaml").write_text("servers: {}\n", encoding="utf-8")
        (self.local / "validation.yaml").write_text("checks: {}\nsuites: {}\n", encoding="utf-8")
        self.old = {
            key: os.environ.get(key)
            for key in (
                "AGENELF_ROOT",
                "AGENELF_LOCAL_DIR",
                "AGENELF_SELF_DIR",
                "AGENELF_VALIDATION_FILE",
                "AGENELF_SERVERS_FILE",
                "OPENAI_API_KEY",
            )
        }
        os.environ["AGENELF_ROOT"] = str(self.root)
        os.environ["AGENELF_LOCAL_DIR"] = str(self.local)
        os.environ["AGENELF_SELF_DIR"] = str(self.local / "self")
        os.environ["AGENELF_VALIDATION_FILE"] = str(self.local / "validation.yaml")
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ.pop("AGENELF_SERVERS_FILE", None)

    def tearDown(self):
        for key, value in self.old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def _write_validation_failure(self, index: int) -> None:
        validation_id = f"val-{index:016x}"
        request = self.root / "data" / "validation-requests" / f"{validation_id}.json"
        result = self.root / "data" / "validation-results" / f"{validation_id}.json"
        request.parent.mkdir(parents=True, exist_ok=True)
        result.parent.mkdir(parents=True, exist_ok=True)
        request.write_text(
            json.dumps(
                {
                    "id": validation_id,
                    "operation": "run_check",
                    "target": "api-health",
                    "created_at": f"2026-01-01T00:00:0{index}+00:00",
                }
            ),
            encoding="utf-8",
        )
        result.write_text(
            json.dumps(
                {
                    "id": validation_id,
                    "status": "failed",
                    "summary": "0/1 个检查通过，1 个失败",
                    "finished_at": f"2026-01-01T00:00:0{index}+00:00",
                }
            ),
            encoding="utf-8",
        )

    def test_snapshot_assessment_and_roadmap_use_trusted_evidence(self):
        self._write_validation_failure(1)
        self._write_validation_failure(2)
        config = {
            "mock": True,
            "runtime_root": str(self.root),
            "local_dir": str(self.local),
            "self_dir": str(self.local / "self"),
            "skills_dir": str(PROJECT_ROOT / "skills"),
            "memory_path": str(self.local / "memory" / "memory.json"),
            "local_profile_path": str(self.local / "profile.yaml"),
            "local_preferences_path": str(self.local / "preferences.yaml"),
            "local_context_dir": str(self.local / "context"),
            "servers_path": str(self.local / "servers.yaml"),
            "validation_path": str(self.local / "validation.yaml"),
            "agent": {"name": "Agenelf", "history_max_messages": 4},
            "self_development": {
                "auto_reflect_every_episodes": 999,
                "min_reflection_interval_seconds": 0,
            },
        }
        agent = Agent(config)
        snapshot = agent.self_snapshot()
        self.assertEqual(
            snapshot["capability_health"]["scorecards"]["software.validation"]["health"],
            "degraded",
        )
        assessment = agent.self_assess()
        self.assertTrue(
            any(item["code"] == "capability_degraded:software.validation" for item in assessment["findings"])
        )
        agent.create_improvement_intention(
            title="修复 API 验证",
            rationale="连续失败",
            priority="P1",
            acceptance_criteria=["api-health 通过"],
        )
        roadmap = agent.improvement_roadmap()
        self.assertEqual(roadmap["recommended"]["title"], "修复 API 验证")
        self.assertFalse(roadmap["consciousness_claim"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
