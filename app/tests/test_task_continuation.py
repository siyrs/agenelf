from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from skills import task_continuation


class TaskContinuationTest(unittest.TestCase):
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

    def test_checkpoint_is_persistent_redacted_and_idempotent(self) -> None:
        first = task_continuation.checkpoint(
            "升级 Docker 技能后继续修 VPN",
            "读取 vless://secret@example.com 并修复 token=abc",
            expires_minutes=60,
            max_attempts=2,
        )
        second = task_continuation.checkpoint(
            "升级 Docker 技能后继续修 VPN",
            "读取 vless://secret@example.com 并修复 token=abc",
            expires_minutes=60,
            max_attempts=2,
        )
        self.assertEqual(first["id"], second["id"])
        stored = json.loads(
            (self.root / "data" / "continuations" / "pending.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn("vless://[REDACTED]", stored["resume_prompt"])
        self.assertNotIn("secret@example.com", json.dumps(stored))
        self.assertNotIn("token=abc", json.dumps(stored))

    def test_claim_is_single_attempt_and_finish_does_not_loop(self) -> None:
        created = task_continuation.checkpoint("task", "continue", max_attempts=2)
        claimed = task_continuation.claim_pending()
        self.assertEqual(claimed["id"], created["id"])
        self.assertEqual(claimed["status"], "running")
        self.assertEqual(claimed["attempt_count"], 1)
        self.assertIsNone(task_continuation.claim_pending())
        result = task_continuation.finish_attempt(created["id"], result="still working")
        self.assertEqual(result["status"], "attempted")
        self.assertIsNone(task_continuation.claim_pending())

    def test_retry_respects_max_attempts(self) -> None:
        created = task_continuation.checkpoint("task", "continue", max_attempts=1)
        task_continuation.claim_pending()
        task_continuation.finish_attempt(created["id"], error="boom")
        state = task_continuation.status()
        self.assertEqual(state["status"], "failed")
        with self.assertRaisesRegex(ValueError, "次数"):
            task_continuation.retry(created["id"])

    def test_complete_archives_evidence(self) -> None:
        created = task_continuation.checkpoint("task", "continue")
        completed = task_continuation.complete(
            created["id"], ["op-0123456789abcdef", "test report"]
        )
        self.assertEqual(completed["status"], "completed")
        history = self.root / "data" / "continuations" / "history" / f"{created['id']}.json"
        self.assertTrue(history.is_file())
        archived = json.loads(history.read_text(encoding="utf-8"))
        self.assertEqual(archived["evidence"], ["op-0123456789abcdef", "test report"])

    def test_tool_status_does_not_return_full_resume_prompt(self) -> None:
        created = task_continuation.checkpoint("task", "private resume instructions")
        output = json.loads(task_continuation.execute("task_continuation_status", {}))
        self.assertTrue(output["ok"])
        self.assertEqual(output["continuation"]["id"], created["id"])
        self.assertNotIn("resume_prompt", output["continuation"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
