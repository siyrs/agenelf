from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core import validation


class ValidationQueueTest(unittest.TestCase):
    def test_submit_and_combine_trusted_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = validation.submit_validation(
                "run_check",
                "api-health",
                "检查 API",
                root=root,
            )
            self.assertTrue(request["id"].startswith("val-"))
            self.assertEqual(request["risk"], "read")
            state = validation.get_validation(request["id"], root=root)
            self.assertEqual(state["status"], "queued")

            result_path = root / "data" / "validation-results" / f"{request['id']}.json"
            result_path.parent.mkdir(parents=True)
            result_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "id": request["id"],
                        "status": "succeeded",
                        "summary": "1/1 个检查通过",
                    }
                ),
                encoding="utf-8",
            )
            finished = validation.wait_for_validation(request["id"], root=root)
            self.assertEqual(finished["status"], "succeeded")
            self.assertIn("result", finished)

    def test_rejects_free_form_operation_and_bad_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                validation.submit_validation("curl_anything", "x", "bad", root=root)
            with self.assertRaises(ValueError):
                validation.get_validation("../../escape", root=root)


if __name__ == "__main__":
    unittest.main(verbosity=2)
