"""内嵌 Web 控制台相关 API 的单元测试。

覆盖：
- /ui          StaticFiles 托管（web/ 存在时挂载；不存在时 warning 容错不崩溃）
- /            未鉴权重定向到 /ui/
- /approvals   只读待审批列表（空目录容错、含数据时字段完整、hint 指向 CLI/宿主机审批）
- /chat/history 进程内会话历史最近 N 条（limit 默认 50、上限 200）

兼容两种运行方式：
    python -m unittest tests.test_api_console
    python tests/test_api_console.py
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class _ApiTestBase(unittest.TestCase):
    """公共环境：mock LLM、隔离 AGENELF_ROOT、显式 API token。"""

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

        from fastapi.testclient import TestClient

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


class RootRedirectTest(_ApiTestBase):
    def test_root_redirects_to_ui_without_auth(self):
        client = type(self.client)(self.api.app)  # 无 token 的客户端
        response = client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/ui/")


class WebConsoleMountTest(_ApiTestBase):
    """挂载逻辑：web/ 存在时挂载 /ui，不存在时 warning 容错。"""

    def tearDown(self) -> None:
        # 恢复导入时的环境并重载模块，避免影响其他测试
        super().tearDown()
        importlib.reload(self.api)

    def _reload_with_web(self, web_dir: Path | None):
        if web_dir is not None:
            (web_dir / "assets").mkdir(parents=True, exist_ok=True)
            (web_dir / "index.html").write_text(
                "<html><body>agenelf-console</body></html>", encoding="utf-8"
            )
            (web_dir / "assets" / "app.css").write_text("body{}", encoding="utf-8")
        return importlib.reload(self.api)

    def test_mount_serves_index_and_assets(self):
        web_dir = self.root / "web"
        api = self._reload_with_web(web_dir)
        try:
            from fastapi.testclient import TestClient

            client = TestClient(api.app)
            index = client.get("/ui/")
            self.assertEqual(index.status_code, 200)
            self.assertIn("agenelf-console", index.text)
            asset = client.get("/ui/assets/app.css")
            self.assertEqual(asset.status_code, 200)
            root = client.get("/", follow_redirects=False)
            self.assertEqual(root.status_code, 307)
        finally:
            importlib.reload(api)

    def test_missing_web_dir_warns_and_does_not_crash(self):
        api = importlib.reload(self.api)
        try:
            with mock.patch.object(
                api,
                "_web_dir_candidates",
                return_value=[self.root / "web-missing"],
            ):
                with self.assertLogs(api.logger, level="WARNING") as captured:
                    mounted = api._mount_web_console()
            self.assertIsNone(mounted)
            self.assertIn("跳过 /ui", "".join(captured.output))
            # API 本身仍可用（不崩溃、鉴权逻辑不受影响）
            from fastapi.testclient import TestClient

            client = TestClient(api.app)
            self.assertEqual(client.get("/health").status_code, 200)
        finally:
            importlib.reload(api)

    def test_web_dir_candidates_order(self):
        candidates = self.api._web_dir_candidates()
        self.assertEqual(candidates[0], self.root / "web")
        self.assertEqual(candidates[2], Path("/agenelf/web"))


class ApprovalsTest(_ApiTestBase):
    def test_empty_directories_return_empty_pending(self):
        response = self.client.get("/approvals")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["pending"], [])
        self.assertIn("/approve", data["hint"])
        self.assertIn("scripts/approve.sh", data["hint"])

    def test_pending_operation_is_listed_readonly(self):
        from core import operations

        request = operations.submit_operation(
            "server.operations",
            "apt_update",
            "primary",
            {},
            operations.RISK_CHANGE,
            "更新 APT 索引",
        )
        response = self.client.get("/approvals")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(len(data["pending"]), 1)
        item = data["pending"][0]
        self.assertEqual(item["operation_id"], request["id"])
        self.assertEqual(item["kind"], "operation")
        self.assertEqual(item["summary"], "更新 APT 索引")
        self.assertEqual(item["risk"], "change")
        self.assertEqual(item["target"], "primary")
        self.assertEqual(item["operation"], "apt_update")
        self.assertTrue(item["created_at"])
        self.assertTrue(item["expires_at"])

    def test_approvals_requires_token(self):
        client = type(self.client)(self.api.app)
        self.assertEqual(client.get("/approvals").status_code, 401)

    def test_no_decision_endpoint_exists(self):
        response = self.client.post("/approvals")
        self.assertEqual(response.status_code, 405)


class ChatHistoryTest(_ApiTestBase):
    def test_history_returns_recent_entries_with_limit(self):
        for message in ("第一条", "第二条", "第三条"):
            response = self.client.post("/chat", json={"message": message})
            self.assertEqual(response.status_code, 200)
        response = self.client.get("/chat/history")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["count"], 6)  # 3 轮 user+assistant
        roles = [entry["role"] for entry in data["history"]]
        self.assertEqual(roles, ["user", "assistant"] * 3)
        self.assertIn("多会话隔离", data["note"])

    def test_history_limit_truncates_to_most_recent(self):
        for message in ("第一条", "第二条"):
            self.client.post("/chat", json={"message": message})
        data = self.client.get("/chat/history", params={"limit": 2}).json()
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["history"][0]["content"], "第二条")

    def test_history_limit_bounds(self):
        self.assertEqual(self.client.get("/chat/history", params={"limit": 0}).status_code, 422)
        self.assertEqual(self.client.get("/chat/history", params={"limit": 201}).status_code, 422)
        self.assertEqual(self.client.get("/chat/history", params={"limit": 200}).status_code, 200)

    def test_history_requires_token(self):
        client = type(self.client)(self.api.app)
        self.assertEqual(client.get("/chat/history").status_code, 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
