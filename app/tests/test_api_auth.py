from __future__ import annotations

import os
import unittest


class ApiAuthTest(unittest.TestCase):
    def setUp(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError as exc:
            self.skipTest(f"缺少 API 测试依赖：{exc}")
        import api

        self.api = api
        self.old_token = os.environ.get("AGENELF_API_TOKEN")
        os.environ["AGENELF_API_TOKEN"] = "test-secret"
        self.client = TestClient(api.app)

    def tearDown(self):
        if self.old_token is None:
            os.environ.pop("AGENELF_API_TOKEN", None)
        else:
            os.environ["AGENELF_API_TOKEN"] = self.old_token

    def test_chat_requires_configured_token(self):
        response = self.client.post("/chat", json={"message": "你好"})
        self.assertEqual(response.status_code, 401)

    def test_correct_token_reaches_endpoint_validation(self):
        response = self.client.post(
            "/chat",
            headers={"X-Agenelf-Token": "test-secret"},
            json={"message": "   "},
        )
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
