"""会话参数（session_id）相关 API 的单元测试。

覆盖：
- /chat、/chat/stream、GET/DELETE /chat/history 的 session_id 白名单校验；
- 两个 session 的历史互不可见（API 层隔离）；
- DELETE /chat/history 只清空目标桶，无参清默认桶。

兼容两种运行方式：
    python -m unittest tests.test_api_chat_sessions
    python tests/test_api_chat_sessions.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class ApiChatSessionsTest(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from fastapi.testclient import TestClient
        except ImportError as exc:
            self.skipTest(f"缺少依赖，跳过 API 测试：{exc}")
        import api

        self.api = api
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / "data").mkdir()
        self._old_env = {
            key: os.environ.get(key)
            for key in ("AGENELF_MOCK", "AGENELF_ROOT", "OPENAI_API_KEY", "AGENELF_API_TOKEN")
        }
        os.environ["AGENELF_MOCK"] = "1"
        os.environ["AGENELF_ROOT"] = str(self.root)
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ["AGENELF_API_TOKEN"] = "test-token"

        self._orig_load_config = api.load_config

        def _patched_load_config() -> dict:
            config = self._orig_load_config()
            config["memory_path"] = str(self.root / "data" / "memory.json")
            return config

        api.load_config = _patched_load_config
        api._agent = None

        self.client = TestClient(api.app)
        self.client.headers["X-Agenelf-Token"] = "test-token"

    def tearDown(self) -> None:
        self.api.load_config = self._orig_load_config
        self.api._agent = None
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    # ------------------------------------------------------------------
    # 参数校验
    # ------------------------------------------------------------------
    def test_chat_rejects_invalid_session_id(self):
        for bad in ("bad id", "x" * 65, "含有中文", "-leading-dash", "a/b"):
            response = self.client.post(
                "/chat", json={"message": "hi", "session_id": bad}
            )
            self.assertEqual(response.status_code, 400, bad)

    def test_chat_accepts_whitelisted_session_id(self):
        for good in ("alpha", "ops-2", "team.chat", "session_01", "A1"):
            response = self.client.post(
                "/chat", json={"message": "hi", "session_id": good}
            )
            self.assertEqual(response.status_code, 200, good)

    def test_history_endpoints_reject_invalid_session_id(self):
        self.assertEqual(
            self.client.get("/chat/history", params={"session_id": "bad id"}).status_code,
            400,
        )
        self.assertEqual(
            self.client.delete(
                "/chat/history", params={"session_id": "bad id"}
            ).status_code,
            400,
        )

    def test_chat_stream_rejects_invalid_session_id(self):
        response = self.client.post(
            "/chat/stream", json={"message": "hi", "session_id": "bad id"}
        )
        self.assertEqual(response.status_code, 400)

    def test_chat_stream_supports_session_id(self):
        with self.client.stream(
            "POST", "/chat/stream", json={"message": "流式甲", "session_id": "s1"}
        ) as response:
            self.assertEqual(response.status_code, 200)
            for _line in response.iter_lines():
                pass
        data = self.client.get("/chat/history", params={"session_id": "s1"}).json()
        self.assertEqual(data["session_id"], "s1")
        self.assertEqual(data["history"][0]["content"], "流式甲")

    # ------------------------------------------------------------------
    # 多会话隔离与定点清除
    # ------------------------------------------------------------------
    def test_sessions_are_isolated_via_api(self):
        self.client.post("/chat", json={"message": "甲-1", "session_id": "alpha"})
        self.client.post("/chat", json={"message": "乙-1", "session_id": "beta"})
        self.client.post("/chat", json={"message": "默认-1"})

        alpha = self.client.get(
            "/chat/history", params={"session_id": "alpha"}
        ).json()
        beta = self.client.get(
            "/chat/history", params={"session_id": "beta"}
        ).json()
        default = self.client.get("/chat/history").json()

        alpha_text = str(alpha["history"])
        self.assertIn("甲-1", alpha_text)
        self.assertNotIn("乙-1", alpha_text)
        self.assertNotIn("默认-1", alpha_text)
        self.assertIn("乙-1", str(beta["history"]))
        self.assertIn("默认-1", str(default["history"]))
        self.assertEqual(default["session_id"], "default")

    def test_delete_clears_only_target_bucket(self):
        self.client.post("/chat", json={"message": "甲-1", "session_id": "alpha"})
        self.client.post("/chat", json={"message": "乙-1", "session_id": "beta"})

        result = self.client.delete(
            "/chat/history", params={"session_id": "alpha"}
        ).json()
        self.assertEqual(result["cleared"], 2)
        self.assertEqual(result["session_id"], "alpha")

        alpha = self.client.get(
            "/chat/history", params={"session_id": "alpha"}
        ).json()
        beta = self.client.get(
            "/chat/history", params={"session_id": "beta"}
        ).json()
        self.assertEqual(alpha["count"], 0)
        self.assertEqual(beta["count"], 2)

    def test_delete_without_session_id_clears_default_bucket_only(self):
        self.client.post("/chat", json={"message": "默认-1"})
        self.client.post("/chat", json={"message": "乙-1", "session_id": "beta"})

        result = self.client.delete("/chat/history").json()
        self.assertEqual(result["cleared"], 2)
        self.assertEqual(result["session_id"], "default")

        default = self.client.get("/chat/history").json()
        beta = self.client.get(
            "/chat/history", params={"session_id": "beta"}
        ).json()
        self.assertEqual(default["count"], 0)
        self.assertEqual(beta["count"], 2)

    def test_delete_requires_token(self):
        client = type(self.client)(self.api.app)
        self.assertEqual(client.delete("/chat/history").status_code, 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
