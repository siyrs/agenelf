from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path


class ApiValidationTest(unittest.TestCase):
    def setUp(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError as exc:
            self.skipTest(str(exc))
        import api

        self.api = api
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        local = root / "local"
        for directory in (local / "context", local / "memory", local / "self"):
            directory.mkdir(parents=True)
        (local / "profile.yaml").write_text("owner: {name: Test}\n", encoding="utf-8")
        (local / "preferences.yaml").write_text("hobbies: [testing]\n", encoding="utf-8")
        (local / "servers.yaml").write_text("servers: {}\n", encoding="utf-8")
        (local / "validation.yaml").write_text(
            """checks:
  api-health:
    type: http
    description: API
    url: http://127.0.0.1:9999/health
suites:
  smoke: [api-health]
""",
            encoding="utf-8",
        )
        keys = (
            "AGENELF_ROOT",
            "AGENELF_LOCAL_DIR",
            "AGENELF_SELF_DIR",
            "AGENELF_VALIDATION_FILE",
            "AGENELF_MOCK",
            "AGENELF_API_TOKEN",
            "OPENAI_API_KEY",
            "AGENELF_SERVERS_FILE",
        )
        self.old_env = {key: os.environ.get(key) for key in keys}
        os.environ["AGENELF_ROOT"] = str(root)
        os.environ["AGENELF_LOCAL_DIR"] = str(local)
        os.environ["AGENELF_SELF_DIR"] = str(local / "self")
        os.environ["AGENELF_VALIDATION_FILE"] = str(local / "validation.yaml")
        os.environ["AGENELF_MOCK"] = "1"
        os.environ["AGENELF_API_TOKEN"] = "test-token"
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ.pop("AGENELF_SERVERS_FILE", None)
        api._agent = None
        self.client = TestClient(api.app)
        self.headers = {"X-Agenelf-Token": "test-token"}

    def tearDown(self):
        self.api._agent = None
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_catalog_queue_scorecard_and_roadmap_endpoints(self):
        catalog = self.client.get("/validation/catalog", headers=self.headers)
        self.assertEqual(catalog.status_code, 200, catalog.text)
        self.assertEqual(catalog.json()["checks"][0]["name"], "api-health")

        queued = self.client.post(
            "/validation/checks/api-health",
            headers=self.headers,
            json={"wait_seconds": 0},
        )
        self.assertEqual(queued.status_code, 200, queued.text)
        self.assertEqual(queued.json()["status"], "queued")

        snapshot = self.client.get("/self", headers=self.headers)
        self.assertEqual(snapshot.status_code, 200)
        self.assertIn("capability_health", snapshot.json())

        scorecard = self.client.get("/self/capability-health", headers=self.headers)
        self.assertEqual(scorecard.status_code, 200)
        self.assertFalse(scorecard.json()["consciousness_claim"])

        roadmap = self.client.get("/self/roadmap", headers=self.headers)
        self.assertEqual(roadmap.status_code, 200)
        self.assertIn("capability_scorecards", roadmap.json())


if __name__ == "__main__":
    unittest.main(verbosity=2)
