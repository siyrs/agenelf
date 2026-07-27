from __future__ import annotations
import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

class InitRepositoriesTest(unittest.TestCase):
    def test_init_creates_repository_config_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(PROJECT_ROOT / "local", root / "local")
            script = PROJECT_ROOT / "scripts" / "init_local.py"
            source = script.read_text(encoding="utf-8").replace(
                'ROOT = Path(__file__).resolve().parents[1]', f'ROOT = Path({str(root)!r})'
            )
            patched = root / "init_local.py"
            patched.write_text(source, encoding="utf-8")
            spec = importlib.util.spec_from_file_location("init_local_test", patched)
            module = importlib.util.module_from_spec(spec)
            assert spec and spec.loader
            spec.loader.exec_module(module)
            first = module.initialize(migrate=False)
            target = root / "local" / "repositories.yaml"
            self.assertTrue(target.is_file())
            self.assertTrue(first["repositories"])
            target.write_text("schema_version: 1\nrepositories: {keep: {}}\ntest_profiles: {}\n", encoding="utf-8")
            module.initialize(migrate=False)
            self.assertIn("keep", target.read_text(encoding="utf-8"))
            self.assertTrue((root / "code-workspaces").is_dir())
            self.assertTrue((root / "repair-space").is_dir())

if __name__ == "__main__":
    unittest.main(verbosity=2)
