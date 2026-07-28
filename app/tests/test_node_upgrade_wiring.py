from __future__ import annotations

import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


class NodeUpgradeWiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.compose = yaml.safe_load(
            (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        )

    def test_control_plane_image_contains_official_node_and_python(self) -> None:
        source = (ROOT / "Dockerfile.control-plane").read_text(encoding="utf-8")
        self.assertIn("FROM node:24.18.0-bookworm-slim AS node-runtime", source)
        self.assertIn("FROM python:3.12-slim", source)
        self.assertIn("COPY --from=node-runtime /usr/local/ /usr/local/", source)
        self.assertIn("npm_config_ignore_scripts=true", source)
        self.assertNotIn("curl |", source)
        self.assertNotIn("docker.sock", source)

    def test_legacy_agent_stages_node_contract_and_build_fixtures_read_only(self) -> None:
        volumes = [str(item) for item in self.compose["services"]["legacy-agent"]["volumes"]]
        for expected in (
            "./node:/agenelf/repo-source/node:ro",
            "./contracts:/agenelf/repo-source/contracts:ro",
            "./package.json:/agenelf/repo-source/package.json:ro",
            "./package-lock.json:/agenelf/repo-source/package-lock.json:ro",
            "./.node-version:/agenelf/repo-source/.node-version:ro",
            "./Dockerfile.node:/agenelf/repo-source/Dockerfile.node:ro",
            "./Dockerfile.control-plane:/agenelf/repo-source/Dockerfile.control-plane:ro",
        ):
            self.assertIn(expected, volumes)

    def test_self_upgrade_runner_has_explicit_node_targets_but_no_secrets(self) -> None:
        runner = self.compose["services"]["self-upgrade-runner"]
        self.assertEqual(runner["build"]["dockerfile"], "Dockerfile.control-plane")
        self.assertEqual(runner["network_mode"], "none")
        self.assertTrue(runner["read_only"])
        volumes = [str(item) for item in runner["volumes"]]
        for expected in (
            "./node:/agenelf/upgrade-target/node:rw",
            "./contracts:/agenelf/upgrade-target/contracts:rw",
            "./package.json:/agenelf/upgrade-target/package.json:rw",
            "./package-lock.json:/agenelf/upgrade-target/package-lock.json:rw",
            "./.node-version:/agenelf/upgrade-target/.node-version:rw",
            "./Dockerfile.node:/agenelf/upgrade-target/Dockerfile.node:rw",
            "./Dockerfile.control-plane:/agenelf/upgrade-target/Dockerfile.control-plane:rw",
            "./docker-compose.python.yml:/agenelf/upgrade-target/docker-compose.python.yml:rw",
        ):
            self.assertIn(expected, volumes)
        joined = "\n".join(volumes)
        for forbidden in ("local/secrets", "local/profile", "local/memory", "docker.sock"):
            self.assertNotIn(forbidden, joined)
        self.assertFalse(
            any(
                item.startswith("./.git:/")
                or ":/agenelf/upgrade-target/.git:" in item
                for item in volumes
            )
        )

    def test_candidate_contract_test_is_existing_python_test_and_therefore_immutable(self) -> None:
        path = ROOT / "app" / "tests" / "test_node_candidate_contract.py"
        self.assertTrue(path.is_file())
        source = path.read_text(encoding="utf-8")
        self.assertIn(".agenelf-evolution-workspace.json", source)
        self.assertIn('"ci", "--ignore-scripts"', source)
        self.assertIn('"run", "test:node"', source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
