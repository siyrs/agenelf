from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from core import authorized_upgrade


ROOT = Path(__file__).resolve().parents[2]


class NodeSelfUpgradeRunnerTest(unittest.TestCase):
    def test_default_overlay_profiles_python_and_adds_node_runner(self) -> None:
        overlay = yaml.safe_load(
            (ROOT / "compose.override.yaml").read_text(encoding="utf-8")
        )
        services = overlay["services"]
        self.assertEqual(
            services["self-upgrade-runner"]["profiles"],
            ["python-self-upgrade"],
        )
        runner = services["node-self-upgrade-runner"]
        self.assertEqual(runner["build"]["dockerfile"], "Dockerfile.control-plane")
        self.assertIn("node/apps/self-upgrade-runner/src/main.ts", runner["command"])
        self.assertEqual(runner["network_mode"], "none")
        self.assertTrue(runner["read_only"])
        volume_items = [str(item) for item in runner["volumes"]]
        volumes = "\n".join(volume_items)
        for required in (
            "./app-tmp:/agenelf/app-tmp:ro",
            "./data/authorized-upgrades:/agenelf/data/authorized-upgrades:ro",
            "./data/self-upgrade-requests:/agenelf/data/self-upgrade-requests:ro",
            "./data/auth-requests:/agenelf/data/auth-requests:ro",
            "./data/auth-decisions:/agenelf/data/auth-decisions:ro",
            "./data/auth-consumed:/agenelf/data/auth-consumed:rw",
            "./data/self-upgrade-results:/agenelf/data/self-upgrade-results:rw",
            "./data/self-upgrade-backups:/agenelf/data/self-upgrade-backups:rw",
            "./data/self-upgrade-events:/agenelf/data/self-upgrade-events:rw",
            "./node:/agenelf/upgrade-target/node:rw",
            "./policy:/agenelf/upgrade-target/policy:rw",
        ):
            self.assertIn(required, volumes)
        for forbidden in (
            "/agenelf/approval",
            "local/secrets",
            "local/memory",
            "local/self",
            "docker.sock",
        ):
            self.assertNotIn(forbidden, volumes)
        targets = {
            item.split(":", 2)[1]
            for item in volume_items
            if item.count(":") >= 1
        }
        self.assertNotIn("/agenelf/upgrade-target/.git", targets)

    def test_python_rollback_remains_profile_free(self) -> None:
        rollback = yaml.safe_load(
            (ROOT / "docker-compose.python.yml").read_text(encoding="utf-8")
        )
        runner = rollback["services"]["self-upgrade-runner"]
        self.assertNotIn("profiles", runner)
        command = " ".join(runner["command"])
        self.assertIn("scripts/self_upgrade_runner_entry.py", command)
        self.assertNotIn("node-self-upgrade-runner", rollback["services"])

    def test_control_plane_image_defaults_to_node_but_keeps_python_tests(self) -> None:
        source = (ROOT / "Dockerfile.control-plane").read_text(encoding="utf-8")
        self.assertIn("FROM python:3.12-slim", source)
        self.assertIn("COPY --from=node-runtime /usr/local/ /usr/local/", source)
        self.assertIn(
            'CMD ["node", "node/apps/self-upgrade-runner/src/main.ts"]',
            source,
        )
        self.assertIn("npm_config_ignore_scripts=true", source)

    def test_owner_authorized_scope_includes_node_self_upgrade_body(self) -> None:
        plan = authorized_upgrade.make_plan(
            "升级 Node Self-upgrade Runner 和 Dockerfile.control-plane",
            scopes=["node_runners", "node_build", "compose"],
        )
        for expected in (
            "node/apps/self-upgrade-runner/",
            "node/packages/core/src/self-upgrade.ts",
            "node/packages/core/src/self-upgrade-hardening.ts",
            "Dockerfile.control-plane",
            "compose.override.yaml",
            "node/tests/",
            "app/tests/",
        ):
            self.assertIn(expected, plan["allowed_paths"])
        self.assertEqual(
            authorized_upgrade.SELF_UPGRADE_RUNTIME_POLICY_VERSION,
            "owner-authorized-self-upgrade-runtime-v1",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
