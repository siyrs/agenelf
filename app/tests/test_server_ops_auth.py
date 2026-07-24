"""server_ops 高危命令人类授权流程的端到端测试。

覆盖：高危拦截 → 生成 pending 请求 → 模拟人类批准 → 带 auth_id 执行成功；
假 auth_id 拒绝执行；白名单与 confirm 闸门回归。
所有落盘均在临时目录模拟的项目根（AGENELF_ROOT）中进行。
兼容两种运行方式：``pytest tests/test_server_ops_auth.py`` 或
``python tests/test_server_ops_auth.py``。
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

# 保证 python tests/test_server_ops_auth.py 直接运行时也能 import 包
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from skills import server_ops  # noqa: E402


class TestDangerousAuthFlow(unittest.TestCase):
    """高危命令：拦截 → 人类批准 → 核销执行；假授权拒绝。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "data" / "auth-requests").mkdir(parents=True)
        (self.root / "logs").mkdir(parents=True)
        self._old_root_env = os.environ.get("AGENELF_ROOT")
        os.environ["AGENELF_ROOT"] = str(self.root)
        # 待删除的目标文件（放在临时根内，绝不碰真实系统路径）
        self.victim = self.root / "victim.txt"
        self.victim.write_text("待删除", encoding="utf-8")
        self.command = f"rm {self.victim}"

    def tearDown(self):
        if self._old_root_env is None:
            os.environ.pop("AGENELF_ROOT", None)
        else:
            os.environ["AGENELF_ROOT"] = self._old_root_env
        self.tmp.cleanup()

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------
    def _pending_files(self) -> list[Path]:
        return list((self.root / "data" / "auth-requests").glob("*.json"))

    def _approve_as_human(self, request_id: str) -> None:
        """模拟人类在宿主机执行 approve.sh approve 后的 JSON 状态。"""
        path = self.root / "data" / "auth-requests" / f"{request_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        now = datetime.now().astimezone()
        data["status"] = "approved"
        data["decided_at"] = now.isoformat(timespec="seconds")
        data["decided_by"] = "human-tester"
        # 与 approve.sh 一致：批准时刷新 300 秒有效期
        data["expires_at"] = (now + timedelta(seconds=300)).isoformat(timespec="seconds")
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def _extract_request_id(self, text: str) -> str:
        match = re.search(r"auth-[0-9a-f]{12}", text)
        self.assertIsNotNone(match, f"提示中未找到授权请求 ID：{text}")
        return match.group(0)

    # ------------------------------------------------------------------
    # 用例
    # ------------------------------------------------------------------
    def test_dangerous_blocked_creates_pending_request(self):
        result = server_ops.run_shell(self.command)
        self.assertIn("高危命令已拦截", result)
        request_id = self._extract_request_id(result)
        self.assertIn(f"scripts/approve.sh {request_id} approve", result)
        # data/auth-requests/ 出现 pending 文件
        files = self._pending_files()
        self.assertEqual(len(files), 1)
        data = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(data["id"], request_id)
        self.assertEqual(data["status"], "pending")
        self.assertEqual(data["detail"], self.command)
        # 命令未被执行
        self.assertTrue(self.victim.exists())

    def test_approved_auth_executes_once(self):
        # 第一步：拦截并拿到请求 ID
        blocked = server_ops.run_shell(self.command)
        request_id = self._extract_request_id(blocked)
        # 第二步：模拟人类批准
        self._approve_as_human(request_id)
        # 第三步：带 auth_id 重试 → 执行成功，文件被删
        result = server_ops.run_shell(self.command, auth_id=request_id)
        self.assertIn("退出码：0", result)
        self.assertFalse(self.victim.exists())
        # 审计日志有拦截与执行记录
        log = (self.root / "logs" / "audit.log").read_text(encoding="utf-8")
        self.assertIn(request_id, log)
        self.assertIn("dangerous", log)
        # 授权一次性：再次携带同一 auth_id 被拒绝
        again = server_ops.run_shell(self.command, auth_id=request_id)
        self.assertIn("未获授权", again)
        self.assertIn("used", again)

    def test_fake_auth_id_rejected(self):
        result = server_ops.run_shell(self.command, auth_id="auth-000000000000")
        self.assertIn("未获授权", result)
        self.assertIn("not_found", result)
        self.assertTrue(self.victim.exists())

    def test_pending_auth_id_not_executable(self):
        blocked = server_ops.run_shell(self.command)
        request_id = self._extract_request_id(blocked)
        # 未经人类批准直接带 auth_id 重试 → 拒绝
        result = server_ops.run_shell(self.command, auth_id=request_id)
        self.assertIn("未获授权", result)
        self.assertIn("pending", result)
        self.assertTrue(self.victim.exists())


class TestRegression(unittest.TestCase):
    """回归：白名单直放、普通命令 confirm 闸门不变。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_root_env = os.environ.get("AGENELF_ROOT")
        os.environ["AGENELF_ROOT"] = self.tmp.name

    def tearDown(self):
        if self._old_root_env is None:
            os.environ.pop("AGENELF_ROOT", None)
        else:
            os.environ["AGENELF_ROOT"] = self._old_root_env
        self.tmp.cleanup()

    def test_whitelist_uname_runs_directly(self):
        result = server_ops.run_shell("uname")
        self.assertIn("退出码：0", result)
        self.assertTrue("Linux" in result or "MINGW" in result)

    def test_normal_command_still_needs_confirm(self):
        # 目标放在临时目录内，避免污染工作目录、避免重复运行碰撞
        target = Path(self.tmp.name) / "some-dir"
        command = f"mkdir {target}"
        result = server_ops.run_shell(command)
        self.assertIn("确认", result)
        self.assertFalse(target.exists())
        # confirm=True 后放行
        result_ok = server_ops.run_shell(command, confirm=True)
        self.assertIn("退出码：0", result_ok)
        self.assertTrue(target.is_dir())


if __name__ == "__main__":
    unittest.main(verbosity=2)
