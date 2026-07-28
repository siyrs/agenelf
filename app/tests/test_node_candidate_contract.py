from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class NodeAuthorizedUpgradeCandidateContractTest(unittest.TestCase):
    def test_repository_candidate_passes_locked_node_suite(self) -> None:
        repo = Path(__file__).resolve().parents[2]
        marker = repo / ".agenelf-evolution-workspace.json"
        if not marker.is_file():
            self.skipTest("only enforced inside an owner-authorized candidate workspace")

        node = shutil.which("node")
        npm = shutil.which("npm")
        self.assertIsNotNone(node, "trusted control-plane image must contain Node.js")
        self.assertIsNotNone(npm, "trusted control-plane image must contain npm")
        self.assertTrue((repo / "package.json").is_file())
        self.assertTrue((repo / "package-lock.json").is_file())
        self.assertTrue((repo / "node" / "tests").is_dir())

        def ignored(_directory: str, names: list[str]) -> set[str]:
            return {
                name
                for name in names
                if name
                in {
                    ".git",
                    "node_modules",
                    "app-tmp",
                    "data",
                    "logs",
                    "workspace",
                    "local",
                    "__pycache__",
                    ".pytest_cache",
                }
            }

        with tempfile.TemporaryDirectory(
            prefix="agenelf-authorized-node-candidate-"
        ) as temporary:
            candidate = Path(temporary) / "repo"
            shutil.copytree(repo, candidate, ignore=ignored)
            environment = dict(os.environ)
            environment["npm_config_ignore_scripts"] = "true"
            environment["npm_config_audit"] = "false"
            environment["npm_config_fund"] = "false"
            install = subprocess.run(
                [str(npm), "ci", "--ignore-scripts"],
                cwd=candidate,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
            self.assertEqual(
                install.returncode,
                0,
                "locked Node install failed:\n"
                + "\n".join(
                    part for part in (install.stdout, install.stderr) if part
                )[-8000:],
            )
            tests = subprocess.run(
                [str(npm), "run", "test:node"],
                cwd=candidate,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600,
            )
            self.assertEqual(
                tests.returncode,
                0,
                "complete Node candidate suite failed:\n"
                + "\n".join(
                    part for part in (tests.stdout, tests.stderr) if part
                )[-12000:],
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
