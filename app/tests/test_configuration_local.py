from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from core.configuration import load_config


class ConfigurationLocalTest(unittest.TestCase):
    def test_mutable_paths_are_anchored_in_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / "app"
            app.mkdir()
            (app / "config.yaml").write_text(
                "agent:\n  history_max_messages: 12\n", encoding="utf-8"
            )
            local = root / "local"
            local.mkdir()
            (local / "profile.yaml").write_text(
                "owner: {name: Sirius}\n", encoding="utf-8"
            )
            (local / "servers.yaml").write_text("servers: {}\n", encoding="utf-8")
            old_root = os.environ.get("AGENELF_ROOT")
            old_local = os.environ.get("AGENELF_LOCAL_DIR")
            old_servers = os.environ.get("AGENELF_SERVERS_FILE")
            os.environ["AGENELF_ROOT"] = str(root)
            os.environ.pop("AGENELF_LOCAL_DIR", None)
            os.environ.pop("AGENELF_SERVERS_FILE", None)
            try:
                config = load_config(app_dir=app)
            finally:
                if old_root is None:
                    os.environ.pop("AGENELF_ROOT", None)
                else:
                    os.environ["AGENELF_ROOT"] = old_root
                if old_local is None:
                    os.environ.pop("AGENELF_LOCAL_DIR", None)
                else:
                    os.environ["AGENELF_LOCAL_DIR"] = old_local
                if old_servers is None:
                    os.environ.pop("AGENELF_SERVERS_FILE", None)
                else:
                    os.environ["AGENELF_SERVERS_FILE"] = old_servers
            self.assertEqual(Path(config["local_dir"]), local)
            self.assertEqual(
                Path(config["memory_path"]), local / "memory" / "memory.json"
            )
            self.assertEqual(Path(config["servers_path"]), local / "servers.yaml")
            self.assertEqual(Path(config["persona_path"]), local / "profile.yaml")


if __name__ == "__main__":
    unittest.main(verbosity=2)
