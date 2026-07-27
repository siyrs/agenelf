from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class GateLocalProtectionTest(unittest.TestCase):
    def test_privacy_boundary_module_cannot_be_self_modified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            shutil.copy(PROJECT_ROOT / "scripts" / "gate_check.sh", root / "scripts")
            shutil.copy(PROJECT_ROOT / "scripts" / "tree_digest.py", root / "scripts")
            for base in (root / "app-fork", root / "app-tmp"):
                (base / "core").mkdir(parents=True)
                (base / "tests").mkdir()
                (base / "core" / "privacy.py").write_text(
                    "SAFE = True\n", encoding="utf-8"
                )
                (base / "tests" / "test_smoke.py").write_text(
                    "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): self.assertTrue(True)\n",
                    encoding="utf-8",
                )
            (root / "app-tmp" / "core" / "privacy.py").write_text(
                "SAFE = False\n", encoding="utf-8"
            )
            proc = subprocess.run(
                ["bash", str(root / "scripts" / "gate_check.sh"), "evo-local-test"],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertNotEqual(proc.returncode, 0)
            rejected = (
                root
                / "app-tmp"
                / "promote-requests"
                / "evo-local-test"
                / "REJECTED"
            )
            self.assertTrue(rejected.is_file())
            self.assertIn("core/privacy.py", rejected.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
