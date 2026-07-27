from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from skills import software_validation




class FakeAgent:
    def __init__(self):
        self.intentions = []
        self.reflections = []

    def create_improvement_intention(self, **kwargs):
        self.intentions.append(kwargs)
        return {"created": True}

    def reflect_and_sediment(self, **kwargs):
        self.reflections.append(kwargs)
        return {"reflection": True}


class SoftwareValidationSkillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.local = self.root / "local"
        self.local.mkdir()
        self.config = self.local / "validation.yaml"
        self.config.write_text(
            """checks:
  api-health:
    type: http
    description: API 健康
    url: http://10.0.0.2:8080/health
    expected_status: [200]
  db-port:
    type: tcp
    description: 数据库
    host: 10.0.0.3
    port: 5432
suites:
  smoke:
    description: 冒烟
    checks: [api-health, db-port]
""",
            encoding="utf-8",
        )
        self.old = {
            name: os.environ.get(name)
            for name in ("AGENELF_ROOT", "AGENELF_LOCAL_DIR", "AGENELF_VALIDATION_FILE")
        }
        os.environ["AGENELF_ROOT"] = str(self.root)
        os.environ["AGENELF_LOCAL_DIR"] = str(self.local)
        os.environ["AGENELF_VALIDATION_FILE"] = str(self.config)
        self.agent = FakeAgent()
        software_validation.configure_runtime(agent=self.agent)

    def tearDown(self):
        for name, value in self.old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.tmp.cleanup()

    def test_catalog_hides_network_coordinates(self):
        result = json.loads(software_validation.execute("list_validation_checks", {}))
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertIn("api-health", encoded)
        self.assertIn("smoke", encoded)
        self.assertNotIn("10.0.0.2", encoded)
        self.assertNotIn("5432", encoded)
        self.assertNotIn("url", encoded)
        self.assertNotIn("host", encoded)

    def test_check_and_suite_submit_alias_only_requests(self):
        queued = json.loads(
            software_validation.execute(
                "run_validation_check", {"check": "api-health", "wait_seconds": 0}
            )
        )
        self.assertEqual(queued["status"], "queued")
        request = queued["request"]
        self.assertEqual(request["target"], "api-health")
        self.assertEqual(request["parameters"], {})
        request_text = json.dumps(request, ensure_ascii=False)
        self.assertNotIn("10.0.0.2", request_text)

        suite = json.loads(
            software_validation.execute(
                "run_validation_suite", {"suite": "smoke", "wait_seconds": 0}
            )
        )
        self.assertEqual(suite["request"]["operation"], "run_suite")

    def test_failed_result_is_sedimented_as_an_intention(self):
        queued = json.loads(
            software_validation.execute(
                "run_validation_check", {"check": "api-health", "wait_seconds": 0}
            )
        )
        validation_id = queued["id"]
        result_path = self.root / "data" / "validation-results" / f"{validation_id}.json"
        result_path.parent.mkdir(parents=True)
        result_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "id": validation_id,
                    "status": "failed",
                    "target": "api-health",
                    "summary": "0/1 个检查通过，1 个失败",
                }
            ),
            encoding="utf-8",
        )
        state = json.loads(
            software_validation.execute(
                "get_validation_result",
                {"validation_id": validation_id, "wait_seconds": 0},
            )
        )
        self.assertEqual(state["status"], "failed")
        self.assertEqual(len(self.agent.intentions), 1)
        self.assertIn("api-health", self.agent.intentions[0]["title"])
        self.assertEqual(len(self.agent.reflections), 1)

    def test_unknown_alias_is_rejected_without_queueing(self):
        result = software_validation.execute(
            "run_validation_check", {"check": "unknown", "wait_seconds": 0}
        )
        self.assertIn("不存在", result)
        requests = self.root / "data" / "validation-requests"
        self.assertFalse(requests.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
