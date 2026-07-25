from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from skills import workflow_tasks


class WorkflowTasksSkillTest(unittest.TestCase):
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

    def test_capability_contract(self):
        self.assertEqual(workflow_tasks.CAPABILITY_META["id"], "agent.workflow")
        names = {item["function"]["name"] for item in workflow_tasks.TOOLS}
        self.assertIn("workflow_create_task", names)
        self.assertIn("workflow_next_action", names)
        self.assertIn("software.validation", workflow_tasks.CAPABILITY_META["composes_with"])

    def test_create_list_and_next_action(self):
        created = json.loads(
            workflow_tasks.execute(
                "workflow_create_task",
                {
                    "title": "巡检并验证",
                    "owner_goal": "确认服务健康",
                    "steps": [
                        {
                            "title": "巡检",
                            "capability": "server.operations",
                            "operation": "inspect",
                            "target": "primary",
                            "risk": "read",
                        }
                    ],
                    "acceptance_criteria": ["巡检成功"],
                    "evidence_plan": ["保存 op 请求 ID"],
                },
            )
        )
        self.assertTrue(created["ok"], created)
        task_id = created["task"]["id"]
        listed = json.loads(workflow_tasks.execute("workflow_list_tasks", {}))
        self.assertEqual(listed["count"], 1)
        action = json.loads(
            workflow_tasks.execute("workflow_next_action", {"task_id": task_id})
        )
        self.assertTrue(action["ok"])
        self.assertEqual(action["action"], "execute_step")

    def test_invalid_change_task_is_rejected_without_rollback(self):
        result = json.loads(
            workflow_tasks.execute(
                "workflow_create_task",
                {
                    "title": "部署",
                    "owner_goal": "上线新版本",
                    "steps": [{"title": "部署", "risk": "change"}],
                    "acceptance_criteria": ["验证通过"],
                    "evidence_plan": ["保存 val ID"],
                },
            )
        )
        self.assertFalse(result["ok"])
        self.assertIn("rollback_plan", result["error"])
        self.assertFalse(list((self.root / "data" / "tasks").glob("task-*.json")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
