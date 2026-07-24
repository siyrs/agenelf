from __future__ import annotations

import unittest
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class DockerSelfDevelopmentMountTest(unittest.TestCase):
    def test_agent_gets_self_rw_and_runner_does_not_get_it(self):
        compose = yaml.safe_load(
            (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        )
        services = compose["services"]
        agent_volumes = services["agenelf"]["volumes"]
        runner_volumes = services["ops-runner"]["volumes"]
        self_mounts = [
            item for item in agent_volumes if "/agenelf/local/self" in str(item)
        ]
        self.assertEqual(self_mounts, ["./local/self:/agenelf/local/self:rw"])
        self.assertFalse(
            any("/agenelf/local/self" in str(item) for item in runner_volumes)
        )
        self.assertEqual(
            services["agenelf"]["environment"]["AGENELF_SELF_DIR"],
            "/agenelf/local/self",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
