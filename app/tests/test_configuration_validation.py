from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from core.configuration import load_config


class ConfigurationValidationTest(unittest.TestCase):
    def test_validation_config_is_anchored_in_local_and_exported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / "app"
            local = root / "local"
            app.mkdir()
            local.mkdir()
            (app / "config.yaml").write_text("{}\n", encoding="utf-8")
            (local / "validation.yaml").write_text("checks: {}\nsuites: {}\n", encoding="utf-8")
            old = {key: os.environ.get(key) for key in ("AGENELF_ROOT", "AGENELF_LOCAL_DIR", "AGENELF_VALIDATION_FILE")}
            os.environ["AGENELF_ROOT"] = str(root)
            os.environ.pop("AGENELF_LOCAL_DIR", None)
            os.environ.pop("AGENELF_VALIDATION_FILE", None)
            try:
                config = load_config(app_dir=app)
                self.assertEqual(Path(config["validation_path"]), local / "validation.yaml")
                self.assertEqual(os.environ["AGENELF_VALIDATION_FILE"], str(local / "validation.yaml"))
            finally:
                for key, value in old.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value


if __name__ == "__main__":
    unittest.main(verbosity=2)
