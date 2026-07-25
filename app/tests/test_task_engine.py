import tempfile
import unittest
from pathlib import Path

from core.task_engine import TaskEngine


class TaskEngineTest(unittest.TestCase):
    def test_task_lifecycle_requires_valid_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = TaskEngine(Path(tmp))
            task = engine.create("deploy", ["backup", "deploy"], ["health ok"])
            self.assertEqual(task["status"], "planned")
            updated = engine.transition(task["id"], "running", "started")
            self.assertEqual(updated["status"], "running")
            self.assertIn("started", updated["evidence"])

    def test_invalid_state_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = TaskEngine(Path(tmp))
            task = engine.create("x", [], [])
            with self.assertRaises(ValueError):
                engine.transition(task["id"], "unknown")


if __name__ == "__main__":
    unittest.main()
