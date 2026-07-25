from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.task_engine import TaskEngine, TaskEngineError


class TaskEngineTest(unittest.TestCase):
    def _create(self, root: Path, *, change: bool = False):
        engine = TaskEngine(root)
        steps = [
            {
                "title": "巡检目标",
                "capability": "server.operations",
                "operation": "inspect",
                "target": "primary",
                "risk": "read",
            },
            {
                "title": "运行验收",
                "capability": "software.validation",
                "operation": "run_suite",
                "target": "production-smoke",
                "risk": "change" if change else "read",
                "depends_on": [0],
            },
        ]
        task = engine.create(
            title="部署并验证服务",
            owner_goal="服务升级后保持可用",
            steps=steps,
            acceptance_criteria=["全部步骤成功", "冒烟验证通过"],
            evidence_plan=["保存运维请求 ID", "保存验证请求 ID"],
            priority="P1",
            rollback_plan="恢复上一版 compose" if change else "",
            source_channel="mobile",
        )
        return engine, task

    def test_full_lifecycle_requires_trusted_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, task = self._create(Path(tmp))
            task = engine.transition(task["id"], "running", expected_revision=task["revision"])
            task = engine.update_step(
                task["id"],
                0,
                "running",
                expected_revision=task["revision"],
            )
            task = engine.update_step(
                task["id"],
                0,
                "succeeded",
                evidence_kind="operation",
                evidence_reference="op-0123456789abcdef",
                note="巡检成功",
                expected_revision=task["revision"],
            )
            self.assertEqual(engine.next_action(task["id"])["step_index"], 1)
            task = engine.update_step(
                task["id"],
                1,
                "running",
                expected_revision=task["revision"],
            )
            task = engine.update_step(
                task["id"],
                1,
                "succeeded",
                evidence_kind="validation",
                evidence_reference="val-fedcba9876543210",
                note="冒烟通过",
                expected_revision=task["revision"],
            )
            self.assertEqual(task["status"], "verifying")
            completed = engine.transition(
                task["id"], "completed", expected_revision=task["revision"]
            )
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["source_channel"], "mobile")
            self.assertGreaterEqual(
                sum(1 for item in completed["evidence"] if item["trusted"]), 2
            )

    def test_change_task_requires_rollback_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = TaskEngine(Path(tmp))
            with self.assertRaisesRegex(TaskEngineError, "rollback_plan"):
                engine.create(
                    title="重启服务",
                    owner_goal="恢复服务",
                    steps=[{"title": "重启", "risk": "change"}],
                    acceptance_criteria=["服务正常"],
                    evidence_plan=["保存操作证据"],
                )

    def test_step_dependencies_and_approval_are_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, task = self._create(Path(tmp), change=True)
            task = engine.transition(task["id"], "running")
            with self.assertRaisesRegex(TaskEngineError, "依赖"):
                engine.update_step(task["id"], 1, "running")
            with self.assertRaisesRegex(TaskEngineError, "请求 ID"):
                engine.update_step(task["id"], 1, "waiting_approval")
            task = engine.update_step(
                task["id"],
                0,
                "running",
                expected_revision=task["revision"],
            )
            task = engine.update_step(
                task["id"],
                0,
                "succeeded",
                evidence_kind="operation",
                evidence_reference="op-0123456789abcdef",
                expected_revision=task["revision"],
            )
            task = engine.update_step(
                task["id"],
                1,
                "waiting_approval",
                approval_request_id="auth-change-001",
                expected_revision=task["revision"],
            )
            action = engine.next_action(task["id"])
            self.assertEqual(action["action"], "wait_for_approval")

    def test_pause_resume_cancel_and_revision_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine, task = self._create(Path(tmp))
            task = engine.transition(task["id"], "paused")
            stale_revision = task["revision"]
            task = engine.transition(task["id"], "running", expected_revision=stale_revision)
            with self.assertRaisesRegex(TaskEngineError, "版本冲突"):
                engine.transition(
                    task["id"], "paused", expected_revision=stale_revision
                )
            cancelled = engine.transition(
                task["id"],
                "cancelled",
                reason="主人撤销任务",
                expected_revision=task["revision"],
            )
            self.assertEqual(cancelled["status"], "cancelled")
            self.assertTrue(all(step["status"] == "cancelled" for step in cancelled["steps"]))

    def test_completion_rejects_untrusted_note_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = TaskEngine(Path(tmp))
            task = engine.create(
                title="生成报告",
                owner_goal="形成报告",
                steps=[{"title": "撰写", "risk": "read"}],
                acceptance_criteria=["报告存在"],
                evidence_plan=["保存产物"],
            )
            task = engine.transition(task["id"], "running")
            task = engine.update_step(task["id"], 0, "running")
            task = engine.update_step(
                task["id"],
                0,
                "succeeded",
                evidence_kind="note",
                evidence_reference="note-report-finished",
                expected_revision=task["revision"],
            )
            self.assertEqual(task["status"], "verifying")
            with self.assertRaisesRegex(TaskEngineError, "可信"):
                engine.transition(task["id"], "completed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
