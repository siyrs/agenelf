from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path


class ApiSelfDevelopmentTest(unittest.TestCase):
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
        (local / "context").mkdir(parents=True)
        (local / "memory").mkdir()
        (local / "self").mkdir()
        (local / "profile.yaml").write_text(
            "owner: {name: TestOwner}\n", encoding="utf-8"
        )
        (local / "preferences.yaml").write_text(
            "hobbies: [testing]\n", encoding="utf-8"
        )
        (local / "servers.yaml").write_text("servers: {}\n", encoding="utf-8")
        keys = (
            "AGENELF_ROOT",
            "AGENELF_MOCK",
            "OPENAI_API_KEY",
            "AGENELF_API_TOKEN",
            "AGENELF_LOCAL_DIR",
            "AGENELF_SELF_DIR",
            "AGENELF_SERVERS_FILE",
        )
        self.old_env = {key: os.environ.get(key) for key in keys}
        os.environ["AGENELF_ROOT"] = str(root)
        os.environ["AGENELF_LOCAL_DIR"] = str(local)
        os.environ["AGENELF_SELF_DIR"] = str(local / "self")
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

    def test_reflection_and_intention_lifecycle_api(self):
        reflection = self.client.post(
            "/self/reflections",
            headers=self.headers,
            json={"note": "检查持续成长状态", "deep": False},
        )
        self.assertEqual(reflection.status_code, 200, reflection.text)
        self.assertIn("reflection", reflection.json())

        created = self.client.post(
            "/self/intentions",
            headers=self.headers,
            json={
                "title": "改进诊断信息",
                "rationale": "让失败更容易定位",
                "priority": "P1",
                "acceptance_criteria": ["新增回归测试"],
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        intention_id = created.json()["intention"]["id"]

        listed = self.client.get(
            "/self/intentions", headers=self.headers
        )
        self.assertEqual(listed.status_code, 200)
        self.assertTrue(
            any(item["id"] == intention_id for item in listed.json()["intentions"])
        )

        pursued = self.client.post(
            f"/self/intentions/{intention_id}/pursue",
            headers=self.headers,
            json={"apply_changes": False},
        )
        self.assertEqual(pursued.status_code, 200, pursued.text)
        self.assertEqual(pursued.json()["cycle"]["status"], "plan_ready")

        snapshot = self.client.get("/self/development", headers=self.headers)
        self.assertEqual(snapshot.status_code, 200)
        self.assertFalse(
            snapshot.json()["operational_identity"]["consciousness_claim"]
        )

    def test_invalid_priority_is_400(self):
        response = self.client.post(
            "/self/intentions",
            headers=self.headers,
            json={"title": "bad", "priority": "urgent"},
        )
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
