from __future__ import annotations

import unittest
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class DockerValidationTest(unittest.TestCase):
    def test_validation_runner_is_isolated_and_agent_results_are_read_only(self):
        compose = yaml.safe_load((PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        services = compose["services"]
        self.assertIn("validation-runner", services)
        agent_volumes = services["agenelf"]["volumes"]
        runner_volumes = services["validation-runner"]["volumes"]
        self.assertTrue(
            any("validation-results" in item and item.endswith(":ro") for item in agent_volumes)
        )
        self.assertTrue(
            any("local/validation.yaml" in item and item.endswith(":ro") for item in runner_volumes)
        )
        self.assertTrue(
            any("validation-results" in item and item.endswith(":rw") for item in runner_volumes)
        )
        forbidden = ("local/secrets", "local/memory", "local/self", "profile.yaml")
        for value in forbidden:
            self.assertFalse(any(value in item for item in runner_volumes), value)
        self.assertTrue(services["validation-runner"]["read_only"])
        self.assertIn("ALL", services["validation-runner"]["cap_drop"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
