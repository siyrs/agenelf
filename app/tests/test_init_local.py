from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class InitLocalScriptTest(unittest.TestCase):
    def test_migrates_legacy_persona_servers_secrets_and_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            (root / "app" / "persona").mkdir(parents=True)
            (root / "app" / "memory_store").mkdir(parents=True)
            (root / "config").mkdir()
            (root / "secrets").mkdir()
            (root / "local").mkdir()
            shutil.copy(PROJECT_ROOT / "scripts" / "init_local.py", root / "scripts")
            for name in (
                "profile.example.yaml",
                "preferences.example.yaml",
                "servers.example.yaml",
                "context.example.md",
            ):
                shutil.copy(PROJECT_ROOT / "local" / name, root / "local" / name)
            (root / "app" / "persona" / "persona.yaml").write_text(
                "owner: {name: Legacy}\n", encoding="utf-8"
            )
            (root / "config" / "servers.yaml").write_text(
                "servers: {primary: {host: 127.0.0.1}}\n", encoding="utf-8"
            )
            (root / "app" / "memory_store" / "memory.json").write_text(
                '[{"kind":"fact","content":"legacy","ts":1}]\n', encoding="utf-8"
            )
            (root / "secrets" / "known_hosts").write_text(
                "host key\n", encoding="utf-8"
            )
            proc = subprocess.run(
                [sys.executable, str(root / "scripts" / "init_local.py")],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr + proc.stdout)
            status = json.loads(proc.stdout)
            self.assertTrue(status["profile"])
            self.assertTrue(status["servers"])
            self.assertEqual(status["secret_file_count"], 1)
            profile = (root / "local" / "profile.yaml").read_text(encoding="utf-8")
            self.assertIn("Legacy", profile)
            self.assertTrue((root / "local" / "memory" / "memory.json").is_file())
            self.assertTrue((root / "local" / "secrets" / "known_hosts").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
