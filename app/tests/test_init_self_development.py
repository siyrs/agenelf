from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class InitSelfDevelopmentTest(unittest.TestCase):
    def test_init_creates_private_continuity_files_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            (root / "local").mkdir()
            shutil.copy(PROJECT_ROOT / "scripts" / "init_local.py", root / "scripts")
            for name in (
                "profile.example.yaml",
                "preferences.example.yaml",
                "servers.example.yaml",
                "context.example.md",
            ):
                shutil.copy(PROJECT_ROOT / "local" / name, root / "local" / name)
            proc = subprocess.run(
                [
                    sys.executable,
                    str(root / "scripts" / "init_local.py"),
                    "--no-migrate",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr + proc.stdout)
            status = json.loads(proc.stdout)
            self.assertTrue(status["self_dir"])
            self.assertTrue(status["self_state"])
            self.assertTrue(status["self_reflections"])
            self.assertTrue(status["self_intentions"])
            state = root / "local" / "self" / "state.json"
            state.write_text('{"owner_marker": true}\n', encoding="utf-8")
            rerun = subprocess.run(
                [sys.executable, str(root / "scripts" / "init_local.py")],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(rerun.returncode, 0)
            self.assertTrue(
                json.loads(state.read_text(encoding="utf-8"))["owner_marker"]
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
