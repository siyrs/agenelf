from __future__ import annotations

import unittest
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class DockerLocalMountTest(unittest.TestCase):
    def test_agent_never_mounts_local_secrets_but_runner_does(self):
        compose = yaml.safe_load(
            (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        )
        agent_volumes = compose["services"]["agenelf"]["volumes"]
        runner_volumes = compose["services"]["ops-runner"]["volumes"]
        self.assertFalse(any("local/secrets" in item for item in agent_volumes))
        self.assertTrue(
            any("local/secrets" in item and item.endswith(":ro") for item in runner_volumes)
        )
        self.assertTrue(
            any("local/memory" in item and item.endswith(":rw") for item in agent_volumes)
        )
        self.assertFalse(any("local/memory" in item for item in runner_volumes))

    def test_agent_personalization_mounts_are_selective(self):
        compose = yaml.safe_load(
            (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        )
        volumes = compose["services"]["agenelf"]["volumes"]
        expected_items = (
            "local/profile.yaml",
            "local/preferences.yaml",
            "local/context",
            "local/servers.yaml",
        )
        for expected in expected_items:
            self.assertTrue(any(expected in item for item in volumes), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
