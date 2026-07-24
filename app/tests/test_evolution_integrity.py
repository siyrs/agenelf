"""Host gate binds READY to an exact app-tmp tree digest."""

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
        for script in ("gate_check.sh", "promote.sh", "tree_digest.py"):
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

    def test_gate_writes_digest_and_tamper_invalidates_ready(self):
        request_id = "evo-integrity-test"
        gate = self._run("gate_check.sh", request_id)
        self.assertEqual(gate.returncode, 0, gate.stdout + gate.stderr)
        request_dir = self.root / "data" / "promote-requests" / request_id
        digest = (request_dir / "candidate.sha256").read_text(encoding="utf-8").strip()
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertTrue((request_dir / "READY").is_file())
        (self.root / "app-tmp" / "core" / "example.py").write_text("X = 999\n", encoding="utf-8")
        promote = self._run("promote.sh", request_id)
        self.assertNotEqual(promote.returncode, 0)
        self.assertIn("发生变化", promote.stdout + promote.stderr)
        self.assertFalse((request_dir / "READY").exists())
        self.assertTrue((request_dir / "REJECTED").is_file())
        self.assertEqual((self.root / "app" / "core" / "example.py").read_text(encoding="utf-8"), "X = 1\n")

    def test_gate_rejects_security_module_change(self):
        for tree in ("app-fork", "app-tmp"):
            (self.root / tree / "core" / "permissions.py").write_text("SAFE = True\n", encoding="utf-8")
        (self.root / "app-tmp" / "core" / "permissions.py").write_text("SAFE = False\n", encoding="utf-8")
        result = self._run("gate_check.sh", "evo-protected-test")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("安全关键模块发生变化", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
