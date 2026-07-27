from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path


class ApiLocalContextTest(unittest.TestCase):
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
        (local / "profile.yaml").write_text(
            "owner: {name: TestOwner}\n", encoding="utf-8"
        )
        (local / "preferences.yaml").write_text(
            "hobbies: [Python]\n", encoding="utf-8"
        )
        (local / "servers.yaml").write_text("servers: {}\n", encoding="utf-8")
        keys = (
            "AGENELF_ROOT",
            "AGENELF_MOCK",
            "OPENAI_API_KEY",
            "AGENELF_API_TOKEN",
            "AGENELF_LOCAL_DIR",
            "AGENELF_SERVERS_FILE",
        )
        self.old_env = {key: os.environ.get(key) for key in keys}
        os.environ["AGENELF_ROOT"] = str(root)
        os.environ["AGENELF_LOCAL_DIR"] = str(local)
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

    def test_local_status_and_memory_endpoints(self):
        status = self.client.get("/local/status", headers=self.headers)
        self.assertEqual(status.status_code, 200)
        self.assertTrue(status.json()["profile_loaded"])
        saved = self.client.post(
            "/memory",
            headers=self.headers,
            json={"kind": "preference", "content": "喜欢测试"},
        )
        self.assertEqual(saved.status_code, 200)
        found = self.client.get(
            "/memory/search", headers=self.headers, params={"q": "测试"}
        )
        self.assertEqual(found.status_code, 200)
        self.assertTrue(found.json()["results"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
