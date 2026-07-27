"""POST /chat/stream（SSE 流式对话）的单元测试。

事件序列约定：
    event: status  data {"phase": "thinking"}
    event: message data {"delta": "..."}   （按句/段分块，拼接 == 完整 reply）
    event: done    data {"ok": true}
    异常时：       event: error  data {"error": "..."}

参数校验（空消息、非法 channel）在流开始前返回 4xx；未鉴权返回 401。

运行：
    python -m unittest tests.test_api_chat_stream
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _parse_sse(text: str) -> list[tuple[str, dict | None]]:
    """把 SSE 文本解析为 (event, data) 序列。"""
    events: list[tuple[str, dict | None]] = []
    for block in text.split("\n\n"):
        block = block.strip("\n")
        if not block.strip():
            continue
        event = ""
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
        payload = json.loads("\n".join(data_lines)) if data_lines else None
        events.append((event, payload))
    return events


class _StubAgent:
    """固定回复的 agent 替身，避免依赖真实 LLM。"""

    def __init__(self, reply: str):
        self.reply = reply
        self.history: list[dict] = []
        self.calls: list[tuple[str, str]] = []

    def chat(self, message: str, subject: str = "http") -> str:
        self.calls.append((message, subject))
        self.history.append({"role": "user", "content": message})
        self.history.append({"role": "assistant", "content": self.reply})
        return self.reply


class _BoomAgent(_StubAgent):
    def chat(self, message: str, subject: str = "http") -> str:
        raise RuntimeError("后端爆炸")


class ApiChatStreamTest(unittest.TestCase):
    """SSE 流式对话端点测试。"""

    def setUp(self) -> None:
        try:
            from fastapi.testclient import TestClient  # noqa: F401
        except ImportError as exc:
            self.skipTest(f"缺少依赖，跳过 API 测试：{exc}")

        import api  # noqa: E402

        self.api = api
        self._tmp = tempfile.TemporaryDirectory()
        tmp_root = Path(self._tmp.name).resolve()
        (tmp_root / "data").mkdir()
        self._old_env = {
            key: os.environ.get(key)
            for key in ("AGENELF_MOCK", "AGENELF_ROOT", "OPENAI_API_KEY", "AGENELF_API_TOKEN")
        }
        os.environ["AGENELF_MOCK"] = "1"
        os.environ["AGENELF_ROOT"] = str(tmp_root)
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ["AGENELF_API_TOKEN"] = "test-token"
        api._agent = None

        from fastapi.testclient import TestClient

        self.client = TestClient(api.app)
        self.client.headers["X-Agenelf-Token"] = "test-token"

    def tearDown(self) -> None:
        self.api._agent = None
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def _stream(self, payload: dict) -> tuple[int, str, str]:
        """发起流式请求，返回 (status_code, content_type, body)。"""
        with self.client.stream("POST", "/chat/stream", json=payload) as resp:
            body = b"".join(resp.iter_bytes()).decode("utf-8")
            return resp.status_code, resp.headers.get("content-type", ""), body

    # ------------------------------------------------------------------
    # 正常流
    # ------------------------------------------------------------------
    def test_stream_事件顺序与完整reply拼接一致(self):
        reply = "第一句。第二句！第三句？最后一段没有标点"
        self.api._agent = _StubAgent(reply)

        status, content_type, body = self._stream({"message": "你好", "channel": "web"})
        self.assertEqual(status, 200)
        self.assertTrue(content_type.startswith("text/event-stream"), content_type)

        events = _parse_sse(body)
        kinds = [kind for kind, _ in events]
        # status 开头、done 结尾、中间全部是 message
        self.assertEqual(kinds[0], "status")
        self.assertEqual(kinds[-1], "done")
        self.assertTrue(all(kind == "message" for kind in kinds[1:-1]), kinds)

        self.assertEqual(events[0][1], {"phase": "thinking"})
        self.assertEqual(events[-1][1], {"ok": True})

        deltas = [payload["delta"] for _, payload in events[1:-1]]
        self.assertGreaterEqual(len(deltas), 2)  # 确实分块
        self.assertTrue(all(isinstance(d, str) and d for d in deltas))
        self.assertEqual("".join(deltas), reply)

        # channel 透传给 agent
        self.assertEqual(self.api._agent.calls, [("你好", "web")])

    def test_stream_与同步chat回复一致(self):
        reply = "同步与流式应当一致。确实如此！"
        self.api._agent = _StubAgent(reply)

        sync_reply = self.client.post("/chat", json={"message": "hi"}).json()["reply"]
        _, _, body = self._stream({"message": "hi"})
        events = _parse_sse(body)
        streamed = "".join(
            payload["delta"] for kind, payload in events if kind == "message"
        )
        self.assertEqual(streamed, sync_reply)

    def test_stream_空reply仍发出done(self):
        self.api._agent = _StubAgent("")
        status, _, body = self._stream({"message": "你好"})
        self.assertEqual(status, 200)
        kinds = [kind for kind, _ in _parse_sse(body)]
        self.assertEqual(kinds, ["status", "done"])

    def test_stream_超长回复硬切分块(self):
        reply = "很长" * 500  # 1000 字符无标点单句
        self.api._agent = _StubAgent(reply)
        _, _, body = self._stream({"message": "你好"})
        events = _parse_sse(body)
        deltas = [p["delta"] for k, p in events if k == "message"]
        self.assertGreaterEqual(len(deltas), 3)
        self.assertTrue(all(len(d) <= 240 for d in deltas))
        self.assertEqual("".join(deltas), reply)

    # ------------------------------------------------------------------
    # 异常与校验
    # ------------------------------------------------------------------
    def test_stream_agent异常时发出error事件(self):
        self.api._agent = _BoomAgent("")
        status, _, body = self._stream({"message": "你好"})
        self.assertEqual(status, 200)  # 流已开始，异常转为 error 事件
        events = _parse_sse(body)
        kinds = [kind for kind, _ in events]
        self.assertEqual(kinds, ["status", "error"])
        self.assertIn("后端爆炸", events[-1][1]["error"])

    def test_stream_空消息返回400(self):
        resp = self.client.post("/chat/stream", json={"message": "  "})
        self.assertEqual(resp.status_code, 400)

    def test_stream_非法channel返回400(self):
        resp = self.client.post(
            "/chat/stream", json={"message": "你好", "channel": "carrier-pigeon"}
        )
        self.assertEqual(resp.status_code, 400)

    def test_stream_未鉴权返回401(self):
        resp = self.client.post(
            "/chat/stream",
            json={"message": "你好"},
            headers={"X-Agenelf-Token": "wrong"},
        )
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
