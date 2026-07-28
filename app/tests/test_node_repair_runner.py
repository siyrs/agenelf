from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from core import authorized_upgrade


ROOT = Path(__file__).resolve().parents[2]


class NodeRepairRunnerTopologyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.overlay = yaml.safe_load(
            (ROOT / "compose.override.yaml").read_text(encoding="utf-8")
        )
        cls.rollback = yaml.safe_load(
            (ROOT / "docker-compose.python.yml").read_text(encoding="utf-8")
        )

    def test_default_overlay_disables_python_repair_and_adds_node_runner(self) -> None:
        services = self.overlay["services"]
        self.assertEqual(services["repair-runner"]["profiles"], ["python-repair"])
        runner = services["node-repair-runner"]
        self.assertEqual(runner["build"]["dockerfile"], "Dockerfile.repair")
        self.assertEqual(runner["network_mode"], "none")
        self.assertTrue(runner["read_only"])
        self.assertIn("node/apps/repair-runner/src/main.ts", runner["command"])

    def test_node_repair_has_only_required_mounts(self) -> None:
        runner = self.overlay["services"]["node-repair-runner"]
        volumes = "\n".join(str(item) for item in runner["volumes"])
        for required in (
            "./node:/agenelf/node:ro",
            "./local/repositories.yaml:/agenelf/local/repositories.yaml:ro",
            "./code-workspaces:/agenelf/code-workspaces:ro",
            "./repair-space:/agenelf/repair-space:rw",
            "./data/repair-requests:/agenelf/data/repair-requests:ro",
            "./data/repair-results:/agenelf/data/repair-results:rw",
            "./data/repair-locks:/agenelf/data/repair-locks:rw",
            "./data/repair-events:/agenelf/data/repair-events:rw",
            "./data/runner-health:/agenelf/data/runner-health:rw",
        ):
            self.assertIn(required, volumes)
        for forbidden in (
            "/agenelf/app-fork",
            "/agenelf/scripts",
            "/agenelf/policy",
            "/agenelf/approval",
            "local/secrets",
            "local/memory",
            "local/self",
            "docker.sock",
        ):
            self.assertNotIn(forbidden, volumes)

    def test_python_rollback_still_uses_original_runner(self) -> None:
        runner = self.rollback["services"]["repair-runner"]
        command = " ".join(str(item) for item in runner["command"])
        self.assertIn("scripts/repair_runner.py", command)
        self.assertNotIn("profiles", runner)
        self.assertNotIn("node/apps/repair-runner", command)

    def test_repair_image_contains_node_python_and_git_without_remote_installer(self) -> None:
        source = (ROOT / "Dockerfile.repair").read_text(encoding="utf-8")
        self.assertIn("FROM node:24.18.0-bookworm-slim AS node-runtime", source)
        self.assertIn("FROM python:3.12-slim", source)
        self.assertIn("git bash ca-certificates", source)
        self.assertIn("npm_config_ignore_scripts=true", source)
        self.assertNotIn("curl |", source)
        self.assertNotIn("docker.sock", source)

    def test_owner_authorized_upgrade_scope_includes_repair_runtime(self) -> None:
        plan = authorized_upgrade.make_plan(
            "完善 Node Repair Runner 隔离工作区与 Dockerfile.repair",
            scopes=["node_runners", "node_build"],
        )
        for expected in (
            "node/apps/repair-runner/",
            "node/packages/core/src/repair.ts",
            "Dockerfile.repair",
            "node/tests/",
            "app/tests/",
        ):
            self.assertIn(expected, plan["allowed_paths"])
        self.assertEqual(
            authorized_upgrade.REPAIR_UPGRADE_POLICY_VERSION,
            "owner-authorized-node-repair-v1",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
