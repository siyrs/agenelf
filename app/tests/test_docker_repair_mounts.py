from __future__ import annotations
import unittest
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[2]

class DockerRepairMountTest(unittest.TestCase):
    def test_repair_runner_is_isolated(self):
        compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        services = compose["services"]
        self.assertIn("repair-runner", services)
        agent_volumes = "\n".join(services["agenelf"].get("volumes", []))
        runner = services["repair-runner"]
        runner_volumes = "\n".join(runner.get("volumes", []))
        self.assertNotIn("code-workspaces", agent_volumes)
        self.assertIn("./local/repositories.yaml:/agenelf/local/repositories.yaml:ro", agent_volumes)
        self.assertIn("./data/repair-results:/agenelf/data/repair-results:ro", agent_volumes)
        self.assertIn("./code-workspaces:/agenelf/code-workspaces:ro", runner_volumes)
        self.assertIn("./repair-space:/agenelf/repair-space:rw", runner_volumes)
        self.assertNotIn("local/secrets", runner_volumes)
        self.assertNotIn("local/memory", runner_volumes)
        self.assertEqual(runner.get("network_mode"), "none")
        self.assertTrue(runner.get("read_only"))

if __name__ == "__main__":
    unittest.main(verbosity=2)
