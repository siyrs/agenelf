from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class InitModelsTest(unittest.TestCase):
    def test_init_creates_models_without_overwriting_owner_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            (root / "local").mkdir()
            (root / "app" / "persona").mkdir(parents=True)
            shutil.copy(PROJECT_ROOT / "scripts" / "init_local.py", root / "scripts")
            for name in (
                "profile.example.yaml",
                "preferences.example.yaml",
                "servers.example.yaml",
                "validation.example.yaml",
                "models.example.yaml",
                "context.example.md",
            ):
                shutil.copy(PROJECT_ROOT / "local" / name, root / "local" / name)
            first = subprocess.run(
                [sys.executable, str(root / "scripts" / "init_local.py"), "--no-migrate"],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
            status = json.loads(first.stdout)
            self.assertTrue(status["models"])
            models = root / "local" / "models.yaml"
            models.write_text("owner_custom: true\n", encoding="utf-8")
            second = subprocess.run(
                [sys.executable, str(root / "scripts" / "init_local.py"), "--no-migrate"],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(second.returncode, 0, second.stderr + second.stdout)
            self.assertEqual(models.read_text(encoding="utf-8"), "owner_custom: true\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
