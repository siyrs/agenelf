from __future__ import annotations

import unittest
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class DockerModelMountTest(unittest.TestCase):
    def test_models_config_is_agent_only_and_read_only(self):
        compose = yaml.safe_load(
            (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        )
        services = compose["services"]
        agent_volumes = services["agenelf"]["volumes"]
        self.assertTrue(
            any("local/models.yaml" in item and item.endswith(":ro") for item in agent_volumes)
        )
        self.assertEqual(
            services["agenelf"]["environment"]["AGENELF_MODELS_FILE"],
            "/agenelf/local/models.yaml",
        )
        for runner in ("ops-runner", "validation-runner"):
            volumes = services[runner]["volumes"]
            self.assertFalse(any("local/models.yaml" in item for item in volumes))


if __name__ == "__main__":
    unittest.main(verbosity=2)
