from __future__ import annotations

import os
import unittest


class ApiAuthTest(unittest.TestCase):
    """API 认证语义：默认 fail-closed。

    - 配置了 AGENELF_API_TOKEN：无/错 token -> 401，正确 token -> 通过鉴权；
    - 未配置 token：受保护端点一律 503，提示管理员配置；
    - 仅开发模式显式 AGENELF_API_ALLOW_INSECURE=1 时恢复旧的免鉴权行为。
    """

    def setUp(self):
        try:
            from fastapi.testclient import TestClient
        except ImportError as exc:
            self.skipTest(f"缺少 API 测试依赖：{exc}")
        import api

        self.api = api
        self.old_env = {
            key: os.environ.get(key)
            for key in ("AGENELF_API_TOKEN", "AGENELF_API_ALLOW_INSECURE")
        }
        os.environ.pop("AGENELF_API_TOKEN", None)
        os.environ.pop("AGENELF_API_ALLOW_INSECURE", None)
        self.client = TestClient(api.app)

    def tearDown(self):
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_chat_requires_configured_token(self):
        os.environ["AGENELF_API_TOKEN"] = "test-secret"
        response = self.client.post("/chat", json={"message": "你好"})
        self.assertEqual(response.status_code, 401)

    def test_correct_token_reaches_endpoint_validation(self):
        os.environ["AGENELF_API_TOKEN"] = "test-secret"
        response = self.client.post(
            "/chat",
            headers={"X-Agenelf-Token": "test-secret"},
            json={"message": "   "},
        )
        self.assertEqual(response.status_code, 400)

    def test_unset_token_fails_closed_with_503(self):
        response = self.client.post("/chat", json={"message": "你好"})
        self.assertEqual(response.status_code, 503)
        self.assertIn("AGENELF_API_TOKEN", response.json()["detail"])

    def test_unset_token_returns_503_for_all_protected_endpoints(self):
        for path in ("/capabilities", "/status", "/self", "/local/status"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 503, path)

    def test_allow_insecure_flag_restores_legacy_open_behavior(self):
        os.environ["AGENELF_API_ALLOW_INSECURE"] = "1"
        response = self.client.post("/chat", json={"message": "   "})
        # 通过鉴权层，进入端点自身的参数校验（空消息 -> 400）
        self.assertEqual(response.status_code, 400)

    def test_allow_insecure_flag_does_not_bypass_configured_token(self):
        os.environ["AGENELF_API_TOKEN"] = "test-secret"
        os.environ["AGENELF_API_ALLOW_INSECURE"] = "1"
        response = self.client.post("/chat", json={"message": "你好"})
        self.assertEqual(response.status_code, 401)

    def test_health_stays_open_but_minimal(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["version"], self.api.app.version)
        self.assertEqual(set(data), {"status", "version"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
