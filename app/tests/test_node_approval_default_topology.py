from __future__ import annotations

import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


class NodeApprovalDefaultTopologyTest(unittest.TestCase):
    def test_default_compose_imports_stable_graph_without_redefining_services(self) -> None:
        document = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
        self.assertEqual(document, {"include": [{"path": "docker-compose.yml"}]})

    def test_standard_override_promotes_node_approval_and_preserves_key_boundary(self) -> None:
        document = yaml.safe_load(
            (ROOT / "compose.override.yaml").read_text(encoding="utf-8")
        )
        services = document["services"]
        key_init = services["approval-key-init"]
        runner = services["approval-runner"]
        self.assertEqual(key_init["build"]["dockerfile"], "Dockerfile.node")
        self.assertIn("node/apps/approval-key-init/src/main.ts", key_init["command"])
        self.assertEqual(runner["build"]["dockerfile"], "Dockerfile.node")
        self.assertIn("node/apps/approval-runner/src/main.ts", runner["command"])
        agent_volumes = [str(item) for item in services["agenelf"]["volumes"]]
        cli_volumes = [str(item) for item in services["cli"]["volumes"]]
        self.assertIn("./local/prompts:/agenelf/local/prompts:ro", agent_volumes)
        self.assertIn("./local/prompts:/agenelf/local/prompts:ro", cli_volumes)
        self.assertFalse(any("/agenelf/approval" in item for item in agent_volumes))

    def test_upgrade_scope_and_targets_include_default_compose_files(self) -> None:
        source = (ROOT / "app" / "core" / "node_upgrade_policy.py").read_text(
            encoding="utf-8"
        )
        for name in (
            '"compose.yaml"',
            '"compose.override.yaml"',
            '"docker-compose.node-approval.yml"',
        ):
            self.assertIn(name, source)
        mounts = [
            str(item)
            for item in yaml.safe_load(
                (ROOT / "compose.override.yaml").read_text(encoding="utf-8")
            )["services"]["self-upgrade-runner"]["volumes"]
        ]
        self.assertIn(
            "./compose.override.yaml:/agenelf/upgrade-target/compose.override.yaml:rw",
            mounts,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
