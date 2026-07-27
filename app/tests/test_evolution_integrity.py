"""Host gate binds READY to an exact app-tmp tree digest.

gate_check.sh 把产物写入 agent 可写的暂存队列 app-tmp/promote-requests；
promote.sh 在宿主机冻结快照、重算摘要、重跑静态复核后才晋升。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@unittest.skipIf(os.name == "nt", "host evolution scripts are Linux/bash only")
class EvolutionIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for directory in ("app", "app-fork", "app-tmp", "scripts", "data", "logs"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        for script in ("gate_check.sh", "promote.sh", "sync_fork.sh", "tree_digest.py"):
            shutil.copy2(PROJECT_ROOT / "scripts" / script, self.root / "scripts" / script)
        for tree in ("app", "app-fork", "app-tmp"):
            (self.root / tree / "core").mkdir(parents=True)
            (self.root / tree / "tests").mkdir(parents=True)
            (self.root / tree / "core" / "__init__.py").write_text("", encoding="utf-8")
            (self.root / tree / "core" / "example.py").write_text("X = 1\n", encoding="utf-8")
            (self.root / tree / "tests" / "test_example.py").write_text(
                "import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n",
                encoding="utf-8",
            )
        (self.root / "app-tmp" / "core" / "example.py").write_text("X = 2\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, script: str, request_id: str):
        return subprocess.run(
            ["bash", f"scripts/{script}", request_id],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def _staging_dir(self, request_id: str) -> Path:
        return self.root / "app-tmp" / "promote-requests" / request_id

    def test_gate_writes_digest_and_tamper_invalidates_ready(self):
        request_id = "evo-integrity-test"
        gate = self._run("gate_check.sh", request_id)
        self.assertEqual(gate.returncode, 0, gate.stdout + gate.stderr)
        # gate 产物进入 agent 可写的暂存队列，而不是可信的 data/promote-requests
        request_dir = self._staging_dir(request_id)
        digest = (request_dir / "candidate.sha256").read_text(encoding="utf-8").strip()
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertTrue((request_dir / "READY").is_file())
        self.assertFalse(
            (self.root / "data" / "promote-requests" / request_id).exists()
        )
        (self.root / "app-tmp" / "core" / "example.py").write_text("X = 999\n", encoding="utf-8")
        promote = self._run("promote.sh", request_id)
        self.assertNotEqual(promote.returncode, 0)
        self.assertIn("发生变化", promote.stdout + promote.stderr)
        self.assertFalse((request_dir / "READY").exists())
        self.assertTrue((request_dir / "REJECTED").is_file())
        self.assertEqual((self.root / "app" / "core" / "example.py").read_text(encoding="utf-8"), "X = 1\n")

    def test_promote_success_promotes_snapshot_and_records_evidence(self):
        request_id = "evo-success-test"
        gate = self._run("gate_check.sh", request_id)
        self.assertEqual(gate.returncode, 0, gate.stdout + gate.stderr)
        promote = self._run("promote.sh", request_id)
        self.assertEqual(promote.returncode, 0, promote.stdout + promote.stderr)
        # 候选已晋升且运行副本同步
        self.assertEqual(
            (self.root / "app" / "core" / "example.py").read_text(encoding="utf-8"),
            "X = 2\n",
        )
        self.assertEqual(
            (self.root / "app-fork" / "core" / "example.py").read_text(encoding="utf-8"),
            "X = 2\n",
        )
        # 暂存队列不会泄漏进 app/
        self.assertFalse((self.root / "app" / "promote-requests").exists())
        # 晋升证据不可变保存，请求目录已清理
        evidence = self.root / "data" / "promotion-history" / request_id
        self.assertTrue((evidence / "promoted.sha256").is_file())
        self.assertTrue((evidence / "promoted_at").is_file())
        self.assertFalse(self._staging_dir(request_id).exists())

    def test_gate_rejects_security_module_change(self):
        for tree in ("app-fork", "app-tmp"):
            (self.root / tree / "core" / "permissions.py").write_text("SAFE = True\n", encoding="utf-8")
        (self.root / "app-tmp" / "core" / "permissions.py").write_text("SAFE = False\n", encoding="utf-8")
        result = self._run("gate_check.sh", "evo-protected-test")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("安全关键模块发生变化", result.stdout + result.stderr)

    def test_gate_staging_directory_is_configurable(self):
        text = (PROJECT_ROOT / "scripts" / "gate_check.sh").read_text(encoding="utf-8")
        self.assertIn("PROMOTE_REQUESTS_DIR", text)
        self.assertIn("app-tmp/promote-requests", text)

    def test_promote_is_hardened_against_forgery_races_and_portability(self):
        text = (PROJECT_ROOT / "scripts" / "promote.sh").read_text(encoding="utf-8")
        # 并发互斥
        self.assertIn("flock -n", text)
        # 快照冻结消除 TOCTOU，摘要与同步都基于快照
        self.assertIn("cp -a", text)
        self.assertIn("SNAPSHOT_DIR", text)
        # 宿主机复核：静态 re-gate + docker 隔离重跑候选测试，可显式跳过
        self.assertIn("AGENELF_PROMOTE_SKIP_REGATE", text)
        self.assertIn("AGENELF_GATE_SKIP_TESTS", text)
        self.assertIn("--network none", text)
        self.assertIn("--read-only", text)
        self.assertIn("run_candidate_tests.py", text)
        # 可移植写法：不依赖 GNU 扩展
        self.assertNotIn("--iso-8601", text)
        self.assertNotIn("--warning=no-file-changed", text)
        self.assertIn("date -u +%Y-%m-%dT%H:%M:%SZ", text)

    def test_watcher_rechecks_staging_before_trusting(self):
        text = (PROJECT_ROOT / "scripts" / "watcher.sh").read_text(encoding="utf-8")
        self.assertIn("PROMOTE_REQUESTS_DIR", text)
        self.assertIn("app-tmp/promote-requests", text)
        self.assertIn("data/promote-requests", text)
        self.assertIn("candidate.sha256", text)
        self.assertNotIn("--iso-8601", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
