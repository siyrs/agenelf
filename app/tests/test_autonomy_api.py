"""HTTP exposure for observable self-model and plan-only autonomy cycles."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class AutonomyApiTest(unittest.TestCase):
    def setUp(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError as exc:
            self.skipTest(f"缺少 FastAPI 测试依赖：{exc}")
        import api

        self.api = api
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "data").mkdir()
        self.old = {key: os.environ.get(key) for key in ("AGENELF_MOCK", "AGENELF_ROOT", "OPENAI_API_KEY", "AGENELF_API_TOKEN")}
        os.environ["AGENELF_MOCK"] = "1"
        os.environ["AGENELF_ROOT"] = str(self.root)
        os.environ.pop("OPENAI_API_KEY", None)
        # API 默认 fail-closed：显式配置 token 并随请求携带
        os.environ["AGENELF_API_TOKEN"] = "test-token"
        self.original_load = api.load_config

        def load_config():
            config = self.original_load()
            config["memory_path"] = str(self.root / "data" / "memory.json")
            return config

        api.load_config = load_config
        api._agent = None
        self.client = TestClient(api.app)
        self.client.headers["X-Agenelf-Token"] = "test-token"

    def tearDown(self):
        self.api.load_config = self.original_load
        self.api._agent = None
        for key, value in self.old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_self_endpoint_is_observable_not_consciousness_claim(self):
        response = self.client.get("/self")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["identity"]["consciousness_claim"])
        self.assertIn("safety_invariants", data)

    def test_plan_only_cycle_is_persisted(self):
        response = self.client.post("/autonomy/cycles", json={"goal": "检查自身能力缺口", "apply_changes": False})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["status"], "plan_ready")
        cycle_id = data["id"]
        status = self.client.get(f"/autonomy/cycles/{cycle_id}")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["goal"], "检查自身能力缺口")


if __name__ == "__main__":
    unittest.main(verbosity=2)
