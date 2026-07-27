from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import cli


class CliConfigTest(unittest.TestCase):
    def test_default_config_is_anchored_to_cli_file_not_cwd(self):
        old_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                config = cli.load_config(None)
            finally:
                os.chdir(old_cwd)
        self.assertTrue(Path(config["skills_dir"]).is_absolute())
        self.assertEqual(Path(config["skills_dir"]), Path(cli.__file__).resolve().parent / "skills")
        self.assertEqual(config["agent"]["history_max_messages"], 12)


if __name__ == "__main__":
    unittest.main(verbosity=2)
