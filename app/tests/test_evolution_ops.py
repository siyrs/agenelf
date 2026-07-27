"""evolution_ops 技能的单元测试。

在临时目录中模拟容器内完整目录布局：
    app-fork/   只读运行代码副本（内含可通过的测试）
    app-tmp/    可写暂存区
    scripts/    安全脚本（gate_check.sh 用最小可用桩：写 READY 文件并 exit 0）
    data/ logs/ 数据与日志目录
设置 AGENELF_ROOT 指向该临时根后，覆盖：
- begin              会话创建、app-tmp 有代码副本
- write_file         写入成功且拒绝 ../ 逃逸
- run_tests          返回摘要并更新会话状态
- request_promotion  触发桩脚本并报告成功（含未测先升的拒绝分支）
- 越权               尝试往 app-fork 路径写入被拒绝

兼容两种运行方式：
    python -m unittest tests.test_evolution_ops
    python tests/test_evolution_ops.py
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# 保证无论从哪个目录运行都能导入被测的 skills 包
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from skills import evolution_ops  # noqa: E402

# app-fork/ 内的占位代码与被复制的可通过测试
FORK_CODE = 'def answer():\n    return 42\n'

FORK_TEST = '''import unittest

from core.dummy import answer


class DummyTest(unittest.TestCase):
    def test_answer(self):
        self.assertEqual(answer(), 42)


if __name__ == "__main__":
    unittest.main()
'''

# 最小可用的 gate_check.sh 桩：写 READY 标记并 exit 0
GATE_STUB = '''#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ID="$1"
mkdir -p "$ROOT/data/promote-requests/$ID"
touch "$ROOT/data/promote-requests/$ID/READY"
echo "gate check passed for $ID"
exit 0
'''


class EvolutionOpsTest(unittest.TestCase):
    """在临时目录布局下测试自我迭代工作流技能。"""

    def setUp(self) -> None:
        # 搭建临时运行时根目录与完整布局
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / "app-fork" / "core").mkdir(parents=True)
        (self.root / "app-fork" / "tests").mkdir(parents=True)
        (self.root / "app-fork" / "core" / "__init__.py").write_text("", encoding="utf-8")
        (self.root / "app-fork" / "core" / "dummy.py").write_text(
            FORK_CODE, encoding="utf-8"
        )
        (self.root / "app-fork" / "tests" / "test_dummy.py").write_text(
            FORK_TEST, encoding="utf-8"
        )
        (self.root / "scripts").mkdir()
        gate = self.root / "scripts" / "gate_check.sh"
        gate.write_text(GATE_STUB, encoding="utf-8")
        gate.chmod(0o755)
        (self.root / "app-tmp").mkdir()
        (self.root / "data").mkdir()
        (self.root / "logs").mkdir()

        # 设置 AGENELF_ROOT 指向临时根，tearDown 中还原
        self._old_root_env = os.environ.get("AGENELF_ROOT")
        os.environ["AGENELF_ROOT"] = str(self.root)

    def tearDown(self) -> None:
        if self._old_root_env is None:
            os.environ.pop("AGENELF_ROOT", None)
        else:
            os.environ["AGENELF_ROOT"] = self._old_root_env
        self._tmp.cleanup()

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------
    def _begin(self) -> str:
        """开始一次迭代会话并返回结果文本。"""
        return evolution_ops.execute("evolution_begin", {"goal": "测试目标"})

    def _session(self) -> dict:
        """读取当前会话记录。"""
        return json.loads(
            (self.root / "data" / "evolution-session.json").read_text(
                encoding="utf-8"
            )
        )

    # ------------------------------------------------------------------
    # 用例
    # ------------------------------------------------------------------
    def test_begin_创建会话并复制代码副本(self):
        result = self._begin()
        self.assertIn("已开始", result)

        # 会话记录已创建，初始状态为 editing
        session = self._session()
        self.assertEqual(session["goal"], "测试目标")
        self.assertEqual(session["status"], "editing")
        self.assertFalse(session["tests_passed"])
        self.assertTrue(session["id"].startswith("evo-"))

        # app-tmp 内有 app-fork 的完整副本
        self.assertEqual(
            (self.root / "app-tmp" / "core" / "dummy.py").read_text(
                encoding="utf-8"
            ),
            FORK_CODE,
        )
        self.assertTrue((self.root / "app-tmp" / "tests" / "test_dummy.py").exists())

    def test_begin_清除暂存区残留文件(self):
        """回归：app-tmp 中的残留文件（如上一轮失败的补丁）必须被 begin 清除。"""
        stale_dir = self.root / "app-tmp" / "tests"
        stale_dir.mkdir(parents=True, exist_ok=True)
        stale = stale_dir / "test_stale_leftover.py"
        stale.write_text("# 上一轮迭代的残留\n", encoding="utf-8")

        result = self._begin()
        self.assertIn("已开始", result)
        # 残留文件已清除，且 fork 内容完整镜像
        self.assertFalse(stale.exists())
        self.assertTrue((self.root / "app-tmp" / "tests" / "test_dummy.py").exists())
        self.assertTrue((self.root / "app-tmp" / "core" / "dummy.py").exists())

    def test_write_file_写入成功且拒绝逃逸(self):
        self._begin()

        # 正常写入暂存区
        ok = evolution_ops.execute(
            "evolution_write_file",
            {"path": "skills/new_skill.py", "content": "# 新技能\n"},
        )
        self.assertIn("已写入", ok)
        self.assertEqual(
            (self.root / "app-tmp" / "skills" / "new_skill.py").read_text(
                encoding="utf-8"
            ),
            "# 新技能\n",
        )

        # ../ 逃逸必须被拒绝，且目标文件不存在
        denied = evolution_ops.execute(
            "evolution_write_file",
            {"path": "../app-fork/evil.py", "content": "evil"},
        )
        self.assertIn("写入失败", denied)
        self.assertFalse((self.root / "app-fork" / "evil.py").exists())

    def test_run_tests_返回摘要并更新会话(self):
        self._begin()
        result = evolution_ops.execute("evolution_run_tests", {})
        self.assertIn("测试通过", result)
        self.assertIn("OK", result)

        session = self._session()
        self.assertEqual(session["status"], "tests_passed")
        self.assertTrue(session["tests_passed"])

    def test_request_promotion_触发桩脚本并报告成功(self):
        self._begin()
        evolution_ops.execute("evolution_run_tests", {})
        result = evolution_ops.execute("evolution_request_promotion", {})

        self.assertIn("晋升请求已提交", result)
        session = self._session()
        self.assertEqual(session["status"], "promotion_requested")

        # 桩脚本已被触发：写入了 READY 标记
        ready = (
            self.root / "data" / "promote-requests" / session["id"] / "READY"
        )
        self.assertTrue(ready.exists())

    def test_request_promotion_测试未通过时被拒绝(self):
        self._begin()
        result = evolution_ops.execute("evolution_request_promotion", {})
        self.assertIn("被拒绝", result)
        self.assertIn("测试尚未通过", result)
        # 桩脚本不应被触发
        self.assertFalse((self.root / "data" / "promote-requests").exists())

    def test_越权写入app_fork绝对路径被拒绝(self):
        self._begin()
        target = self.root / "app-fork" / "core" / "dummy.py"
        original = target.read_text(encoding="utf-8")

        # 直接以绝对路径指向 app-fork，必须被硬校验拒绝
        denied = evolution_ops.execute(
            "evolution_write_file",
            {"path": str(target), "content": "已被篡改"},
        )
        self.assertIn("写入失败", denied)
        self.assertIn("app-tmp", denied)
        # 原文件内容保持不变
        self.assertEqual(target.read_text(encoding="utf-8"), original)

        # scripts/ 目录同样受保护
        denied_scripts = evolution_ops.execute(
            "evolution_write_file",
            {"path": "../scripts/gate_check.sh", "content": "evil"},
        )
        self.assertIn("写入失败", denied_scripts)
        self.assertEqual(
            (self.root / "scripts" / "gate_check.sh").read_text(encoding="utf-8"),
            GATE_STUB,
        )

    def test_status_返回会话与晋升记录(self):
        self._begin()
        evolution_ops.execute("evolution_run_tests", {})
        evolution_ops.execute("evolution_request_promotion", {})

        result = evolution_ops.execute("evolution_status", {})
        self.assertIn("当前会话", result)
        self.assertIn("promotion_requested", result)
        self.assertIn("READY", result)


if __name__ == "__main__":
    unittest.main()
