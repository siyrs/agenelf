"""task_board 技能测试：临时目录布局，不触碰真实 workspace。"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from skills import task_board


class TaskBoardSkillTest(unittest.TestCase):
    def setUp(self):
        # 临时运行根：<tmp>/workspace/tasks 为存储目录，<tmp>/logs 为审计目录
        self.root = Path(tempfile.mkdtemp(prefix="task-board-test-"))
        self.store = self.root / "workspace" / "tasks"
        self.store.mkdir(parents=True, exist_ok=True)
        task_board.set_store_dir(self.store)

    def tearDown(self):
        task_board.set_store_dir(None)
        shutil.rmtree(self.root, ignore_errors=True)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _call(self, name: str, args: dict) -> dict:
        return json.loads(task_board.execute(name, args))

    def _create(self, title: str, steps: list[str] | None = None, priority: str = "P2") -> dict:
        result = self._call(
            "task_create",
            {"title": title, "steps": steps or [], "priority": priority},
        )
        self.assertTrue(result["ok"], result)
        return result["task"]

    def _read_board(self) -> dict:
        return json.loads((self.store / "board.json").read_text(encoding="utf-8"))

    # ------------------------------------------------------------------
    # 创建 → 分解 → 逐步 advance → 自动 done
    # ------------------------------------------------------------------
    def test_create_decompose_advance_auto_done(self):
        task = self._create("修复登录页", ["复现问题", "定位代码", "修复并测试"], "P1")
        self.assertEqual(task["status"], "open")
        self.assertEqual(task["priority"], "P1")
        self.assertTrue(task["id"].startswith("task-"))
        self.assertEqual(len(task["steps"]), 3)
        self.assertIsNone(task["done_at"])

        # 逐步推进：pending→doing→done
        r = self._call("task_advance", {"task_id": task["id"], "step_index": 0, "note": "已复现"})
        self.assertEqual(r["step"]["status"], "doing")
        self.assertEqual(r["task_status"], "doing")
        r = self._call("task_advance", {"task_id": task["id"], "step_index": 0})
        self.assertEqual(r["step"]["status"], "done")
        self.assertEqual(r["progress"], "1/3")
        self.assertNotIn("auto_done", r)

        self._call("task_advance", {"task_id": task["id"], "step_index": 1})
        self._call("task_advance", {"task_id": task["id"], "step_index": 1})
        self._call("task_advance", {"task_id": task["id"], "step_index": 2})
        r = self._call("task_advance", {"task_id": task["id"], "step_index": 2})
        self.assertTrue(r["auto_done"])
        self.assertEqual(r["task_status"], "done")

        board = self._read_board()
        saved = board["tasks"][0]
        self.assertEqual(saved["status"], "done")
        self.assertIsNotNone(saved["done_at"])
        self.assertTrue(all(s["status"] == "done" for s in saved["steps"]))

    def test_create_without_steps_returns_hint(self):
        result = self._call("task_create", {"title": "未分解的任务"})
        self.assertTrue(result["ok"])
        self.assertIn("建议", result["hint"])
        self.assertEqual(result["task"]["steps"], [])

    def test_create_validation(self):
        self.assertFalse(self._call("task_create", {"title": ""})["ok"])
        self.assertFalse(
            self._call("task_create", {"title": "x", "priority": "P9"})["ok"]
        )

    def test_advance_errors(self):
        task = self._create("小任务", ["一步"])
        self.assertFalse(
            self._call("task_advance", {"task_id": task["id"], "step_index": 5})["ok"]
        )
        self.assertFalse(
            self._call("task_advance", {"task_id": "bad-id", "step_index": 0})["ok"]
        )
        self.assertFalse(
            self._call("task_advance", {"task_id": "task-20990101-000000-aabbcc", "step_index": 0})["ok"]
        )

    # ------------------------------------------------------------------
    # 列表过滤与进度
    # ------------------------------------------------------------------
    def test_list_filter_and_progress(self):
        t1 = self._create("任务A", ["a1", "a2"])
        self._create("任务B", ["b1"])
        self._call("task_advance", {"task_id": t1["id"], "step_index": 0})

        all_tasks = self._call("task_list", {})
        self.assertEqual(all_tasks["count"], 2)
        doing = self._call("task_list", {"status": "doing"})
        self.assertEqual(doing["count"], 1)
        self.assertEqual(doing["tasks"][0]["progress"], "0/2")
        open_tasks = self._call("task_list", {"status": "open"})
        self.assertEqual(open_tasks["count"], 1)
        bad = self._call("task_list", {"status": "nope"})
        self.assertFalse(bad["ok"])

    # ------------------------------------------------------------------
    # 带证据完成
    # ------------------------------------------------------------------
    def test_complete_with_evidence(self):
        task = self._create("部署修复", ["改代码", "跑测试"])
        evidence = [
            "promotion-history:session-abc123",
            "auth:auth-2026-001",
            "app/tests/test_login.py",
        ]
        r = self._call("task_complete", {"task_id": task["id"], "evidence": evidence})
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], "done")
        self.assertEqual(r["evidence"], evidence)

        saved = self._read_board()["tasks"][0]
        self.assertEqual(saved["status"], "done")
        self.assertEqual(saved["evidence"], evidence)
        self.assertIsNotNone(saved["done_at"])
        # 剩余步骤被一并标记 done
        self.assertTrue(all(s["status"] == "done" for s in saved["steps"]))
        # 终态不可再推进/阻塞/重复完成
        self.assertFalse(
            self._call("task_advance", {"task_id": task["id"], "step_index": 0})["ok"]
        )
        self.assertFalse(
            self._call("task_block", {"task_id": task["id"], "reason": "x"})["ok"]
        )
        self.assertFalse(self._call("task_complete", {"task_id": task["id"]})["ok"])

    # ------------------------------------------------------------------
    # 阻塞与恢复
    # ------------------------------------------------------------------
    def test_block_and_resume(self):
        task = self._create("依赖外部服务", ["等对方", "联调"])
        r = self._call("task_block", {"task_id": task["id"], "reason": "对方接口未就绪"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["status"], "blocked")
        self.assertEqual(r["block_reason"], "对方接口未就绪")

        blocked = self._call("task_list", {"status": "blocked"})
        self.assertEqual(blocked["count"], 1)
        # advance 后恢复 doing
        r = self._call("task_advance", {"task_id": task["id"], "step_index": 0})
        self.assertEqual(r["task_status"], "doing")
        saved = self._read_board()["tasks"][0]
        self.assertEqual(saved["block_reason"], "")
        # 空原因拒绝
        self.assertFalse(
            self._call("task_block", {"task_id": task["id"], "reason": " "})["ok"]
        )

    # ------------------------------------------------------------------
    # 关联改进意向（只存 ID）
    # ------------------------------------------------------------------
    def test_link_intention(self):
        task = self._create("能力缺口", ["分析", "建意向"])
        r = self._call(
            "task_link_intention",
            {"task_id": task["id"], "intention_id": "intent-20260725-120000-ab12cd"},
        )
        self.assertTrue(r["ok"])
        self.assertEqual(r["linked_intention"], "intent-20260725-120000-ab12cd")
        saved = self._read_board()["tasks"][0]
        self.assertEqual(saved["linked_intention"], "intent-20260725-120000-ab12cd")
        # 非 intent- 前缀拒绝
        self.assertFalse(
            self._call(
                "task_link_intention",
                {"task_id": task["id"], "intention_id": "something-else"},
            )["ok"]
        )

    # ------------------------------------------------------------------
    # 有界归档：超过 200 时完成的旧任务进入 board-archive.json
    # ------------------------------------------------------------------
    def test_archive_keeps_board_bounded(self):
        ids = []
        for i in range(200):
            ids.append(self._create(f"批量任务{i}", ["步骤"])["id"])
        self.assertEqual(len(self._read_board()["tasks"]), 200)

        # 完成最旧的 3 条，再创建 2 条 → 总数 202，应归档 2 条已完成旧任务
        for task_id in ids[:3]:
            self._call("task_complete", {"task_id": task_id, "evidence": ["e"]})
        self._create("溢出1", ["s"])
        self._create("溢出2", ["s"])

        board = self._read_board()
        self.assertEqual(len(board["tasks"]), 200)
        archive = json.loads(
            (self.store / "board-archive.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(archive["tasks"]), 2)
        self.assertTrue(all(t["status"] == "done" for t in archive["tasks"]))
        archived_ids = {t["id"] for t in archive["tasks"]}
        self.assertIn(ids[0], archived_ids)
        self.assertIn(ids[1], archived_ids)
        remaining = {t["id"] for t in board["tasks"]}
        self.assertFalse(archived_ids & remaining)

    # ------------------------------------------------------------------
    # 审计留痕
    # ------------------------------------------------------------------
    def test_audit_log_records_actions(self):
        task = self._create("审计验证", ["一步"])
        self._call("task_advance", {"task_id": task["id"], "step_index": 0})
        self._call("task_block", {"task_id": task["id"], "reason": "等待"})
        self._call(
            "task_link_intention",
            {"task_id": task["id"], "intention_id": "intent-x-1"},
        )
        log = (self.root / "logs" / "audit.log").read_text(encoding="utf-8")
        self.assertIn("[task_board] action=create", log)
        self.assertIn("action=advance", log)
        self.assertIn("action=block", log)
        self.assertIn("action=link_intention", log)
        self.assertIn(task["id"], log)

    # ------------------------------------------------------------------
    # board.json 损坏容错：重建空板不崩
    # ------------------------------------------------------------------
    def test_corrupted_board_recovers(self):
        (self.store / "board.json").write_text("{ 这不是合法 JSON", encoding="utf-8")
        result = self._call("task_list", {})
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 0)
        # 损坏后仍可正常创建，主板被重建
        task = self._create("重建后的任务", ["s"])
        board = self._read_board()
        self.assertEqual(len(board["tasks"]), 1)
        self.assertEqual(board["tasks"][0]["id"], task["id"])
        # 结构非法（顶层不是对象）也容错
        (self.store / "board.json").write_text("[1,2,3]", encoding="utf-8")
        self.assertEqual(self._call("task_list", {})["count"], 0)

    def test_unknown_tool(self):
        result = self._call("task_nope", {})
        self.assertFalse(result["ok"])
        self.assertIn("未知工具", result["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
