from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from core.configuration import load_config


class SelfDevelopmentConfigurationTest(unittest.TestCase):
    def test_self_dir_is_anchored_under_local_and_can_be_overridden(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / "app"
            app.mkdir()
            (app / "config.yaml").write_text("{}\n", encoding="utf-8")
            old_root = os.environ.get("AGENELF_ROOT")
            old_local = os.environ.get("AGENELF_LOCAL_DIR")
            old_self = os.environ.get("AGENELF_SELF_DIR")
            try:
                os.environ["AGENELF_ROOT"] = str(root)
                os.environ["AGENELF_LOCAL_DIR"] = str(root / "owner-local")
                os.environ.pop("AGENELF_SELF_DIR", None)
                config = load_config(app_dir=app)
                self.assertEqual(
                    Path(config["self_dir"]),
                    root / "owner-local" / "self",
                )
                os.environ["AGENELF_SELF_DIR"] = str(root / "custom-self")
                overridden = load_config(app_dir=app)
                self.assertEqual(
                    Path(overridden["self_dir"]), root / "custom-self"
                )
            finally:
                for key, value in (
                    ("AGENELF_ROOT", old_root),
                    ("AGENELF_LOCAL_DIR", old_local),
                    ("AGENELF_SELF_DIR", old_self),
                ):
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value


if __name__ == "__main__":
    unittest.main(verbosity=2)
