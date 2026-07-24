"""app/api.py（FastAPI HTTP 入口）的单元测试。

使用 fastapi.testclient.TestClient（依赖 httpx）：
- /health            返回 200 且包含状态、技能数与模型名
- /chat              mock 模式下返回非空 reply
- /evolution/status  返回晋升管道状态结构

依赖缺失处理：fastapi 或 httpx 不可用时，setUp 中即 skipTest。

兼容两种运行方式：
    python -m unittest tests.test_api
    python tests/test_api.py
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

# 保证无论从哪个目录运行都能导入被测的 api 模块
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class ApiTest(unittest.TestCase):
    """HTTP 入口接口测试（强制 MockLLM，无需真实 API Key）。"""

    def setUp(self) -> None:
        # fastapi.testclient 依赖 httpx，缺依赖时整体跳过
        try:
            from fastapi.testclient import TestClient  # noqa: F401
        except ImportError as exc:
            self.skipTest(f"缺少依赖，跳过 API 测试：{exc}")

        import api  # noqa: E402

        self.api = api

        # 强制 mock 模式 + 隔离运行时根目录（memory/会话数据写入临时目录）
        self._tmp = tempfile.TemporaryDirectory()
        tmp_root = Path(self._tmp.name).resolve()
        (tmp_root / "data").mkdir()
        self._old_env = {
            key: os.environ.get(key)
            for key in ("AGENELF_MOCK", "AGENELF_ROOT", "OPENAI_API_KEY")
        }
        os.environ["AGENELF_MOCK"] = "1"
        os.environ["AGENELF_ROOT"] = str(tmp_root)
        # 防止环境中的真实 Key 使 Agent 绕过 mock
        os.environ.pop("OPENAI_API_KEY", None)

        # 包装 load_config：记忆文件重定向到临时目录，保持仓库干净
        self._orig_load_config = api.load_config

        def _patched_load_config() -> dict:
            config = self._orig_load_config()
            config["memory_path"] = str(tmp_root / "data" / "memory.json")
            return config

        api.load_config = _patched_load_config
        # 重置 Agent 单例，确保按本用例的环境变量懒加载
        api._agent = None

        from fastapi.testclient import TestClient

        self.client = TestClient(api.app)

    def tearDown(self) -> None:
        self.api.load_config = self._orig_load_config
        self.api._agent = None
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def test_health_返回200与运行信息(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertIsInstance(data["skills"], int)
        self.assertGreaterEqual(data["skills"], 1)
        # mock 模式下模型固定为 mock-llm
        self.assertEqual(data["model"], "mock-llm")

    def test_chat_mock模式返回reply(self):
        resp = self.client.post("/chat", json={"message": "你好"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("reply", data)
        self.assertIsInstance(data["reply"], str)
        self.assertTrue(data["reply"].strip())

    def test_chat_空消息返回400(self):
        resp = self.client.post("/chat", json={"message": "   "})
        self.assertEqual(resp.status_code, 400)

    def test_evolution_status_返回管道状态结构(self):
        resp = self.client.get("/evolution/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("session", data)
        self.assertIn("promotion_requests", data)
        self.assertIsInstance(data["promotion_requests"], list)


if __name__ == "__main__":
    unittest.main()
