"""GET /tasks 与 GET /tasks/{task_id} 的单元测试。

覆盖：
- 双来源合并（workspace/tasks/board.json 任务板 + data/tasks/ 治理引擎），
  每项标注 source: board | engine；
- 目录/文件缺失时容错为空列表；
- status 查询参数过滤；
- 详情端点返回完整记录（engine 含 events 审计历史），非法 ID 400、不存在 404；
- 鉴权 fail-closed（无 token 401）。

运行：
    python -m unittest tests.test_api_tasks
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


class ApiTasksTest(unittest.TestCase):
    """任务只读端点测试（隔离临时运行根，无需真实 API Key）。"""

    def setUp(self) -> None:
        try:
            from fastapi.testclient import TestClient  # noqa: F401
        except ImportError as exc:
            self.skipTest(f"缺少依赖，跳过 API 测试：{exc}")

        import api  # noqa: E402

        self.api = api
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / "data").mkdir()
        (self.root / "workspace" / "tasks").mkdir(parents=True)

        self._old_env = {
            key: os.environ.get(key)
            for key in ("AGENELF_MOCK", "AGENELF_ROOT", "OPENAI_API_KEY", "AGENELF_API_TOKEN")
        }
        os.environ["AGENELF_MOCK"] = "1"
        os.environ["AGENELF_ROOT"] = str(self.root)
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ["AGENELF_API_TOKEN"] = "test-token"

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

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _write_board(self, tasks: list[dict]) -> None:
        path = self.root / "workspace" / "tasks" / "board.json"
        path.write_text(
            json.dumps({"tasks": tasks}, ensure_ascii=False), encoding="utf-8"
        )

    def _board_task(self, task_id: str = "task-20240101-abcdef", **overrides) -> dict:
        task = {
            "id": task_id,
            "title": "修复登录页",
            "steps": [
                {"text": "复现问题", "status": "done", "note": ""},
                {"text": "定位代码", "status": "pending", "note": ""},
            ],
            "status": "doing",
            "priority": "P1",
            "created_at": "2024-01-01T00:00:00+00:00",
            "updated_at": "2024-01-02T00:00:00+00:00",
            "done_at": None,
            "evidence": [],
            "linked_intention": None,
            "block_reason": "",
        }
        task.update(overrides)
        return task

    def _engine_task(self) -> dict:
        from core.task_engine import TaskEngine

        engine = TaskEngine(self.root)
        return engine.create(
            title="治理任务：升级验证",
            owner_goal="提升验证覆盖率",
            steps=[{"title": "运行验证套件", "risk": "read"}],
            acceptance_criteria=["套件全绿"],
            evidence_plan=["validation 结果 ID"],
            priority="P2",
        )

    # ------------------------------------------------------------------
    # 列表
    # ------------------------------------------------------------------
    def test_tasks_目录缺失时返回空列表(self):
        # 全新临时根：workspace 与 data/tasks 均不存在
        import shutil

        shutil.rmtree(self.root / "workspace")
        resp = self.client.get("/tasks")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["tasks"], [])
        self.assertEqual(data["count"], 0)
        self.assertIn("board", data["sources"])
        self.assertIn("engine", data["sources"])

    def test_tasks_合并双来源并标注source(self):
        self._write_board([self._board_task()])
        engine_task = self._engine_task()

        resp = self.client.get("/tasks")
        self.assertEqual(resp.status_code, 200)
        tasks = {item["id"]: item for item in resp.json()["tasks"]}
        self.assertEqual(len(tasks), 2)

        board = tasks["task-20240101-abcdef"]
        self.assertEqual(board["source"], "board")
        self.assertEqual(board["title"], "修复登录页")
        self.assertEqual(board["status"], "doing")
        self.assertEqual(board["priority"], "P1")
        self.assertEqual(board["progress"], "1/2")
        self.assertEqual(board["updated_at"], "2024-01-02T00:00:00+00:00")

        engine = tasks[engine_task["id"]]
        self.assertEqual(engine["source"], "engine")
        self.assertEqual(engine["title"], "治理任务：升级验证")
        self.assertEqual(engine["status"], "planned")
        self.assertEqual(engine["progress"], "0/1")
        self.assertIn("trusted_evidence", engine)

    def test_tasks_按updated_at倒序(self):
        self._write_board(
            [
                self._board_task("task-20240101-aaaaaa", updated_at="2024-01-01T00:00:00+00:00"),
                self._board_task("task-20240101-bbbbbb", updated_at="2024-01-03T00:00:00+00:00"),
            ]
        )
        resp = self.client.get("/tasks")
        ids = [item["id"] for item in resp.json()["tasks"]]
        self.assertEqual(ids, ["task-20240101-bbbbbb", "task-20240101-aaaaaa"])

    def test_tasks_status过滤(self):
        self._write_board(
            [
                self._board_task("task-20240101-aaaaaa", status="doing"),
                self._board_task("task-20240101-bbbbbb", status="done"),
            ]
        )
        resp = self.client.get("/tasks", params={"status": "done"})
        self.assertEqual(resp.status_code, 200)
        tasks = resp.json()["tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["id"], "task-20240101-bbbbbb")

    def test_tasks_损坏的board文件容错为空(self):
        path = self.root / "workspace" / "tasks" / "board.json"
        path.write_text("{not-json", encoding="utf-8")
        resp = self.client.get("/tasks")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["tasks"], [])

    def test_tasks_未鉴权返回401(self):
        resp = self.client.get("/tasks", headers={"X-Agenelf-Token": "wrong"})
        self.assertEqual(resp.status_code, 401)

    # ------------------------------------------------------------------
    # 详情
    # ------------------------------------------------------------------
    def test_task_detail_board任务返回完整记录(self):
        self._write_board([self._board_task()])
        resp = self.client.get("/tasks/task-20240101-abcdef")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["source"], "board")
        task = data["task"]
        self.assertEqual(task["id"], "task-20240101-abcdef")
        self.assertEqual(len(task["steps"]), 2)
        self.assertEqual(task["steps"][0]["status"], "done")

    def test_task_detail_engine任务含审计事件(self):
        engine_task = self._engine_task()
        resp = self.client.get(f"/tasks/{engine_task['id']}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["source"], "engine")
        task = data["task"]
        self.assertEqual(task["id"], engine_task["id"])
        # 审计/历史字段：创建事件与证据列表
        self.assertIsInstance(task.get("events"), list)
        self.assertEqual(task["events"][0]["event"], "created")
        self.assertIsInstance(task.get("evidence"), list)

    def test_task_detail_不存在返回404(self):
        resp = self.client.get("/tasks/task-0000000000000000")
        self.assertEqual(resp.status_code, 404)

    def test_task_detail_非法ID返回400(self):
        # 注意：含 "/.." 的路径会被 HTTP 客户端归一化，到不了路由，故用 URL 安全但非法的 ID
        for bad in ("task-..", "not-a-task", "task-", "task-a b"):
            resp = self.client.get(f"/tasks/{bad}")
            self.assertEqual(resp.status_code, 400, bad)

    def test_task_detail_未鉴权返回401(self):
        resp = self.client.get(
            "/tasks/task-20240101-abcdef", headers={"X-Agenelf-Token": "wrong"}
        )
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
