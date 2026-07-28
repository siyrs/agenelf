from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from core import authorized_upgrade


ROOT = Path(__file__).resolve().parents[2]


class NodeChangeOpsRunnerTest(unittest.TestCase):
    def test_default_overlay_separates_read_change_and_python_ops(self) -> None:
        overlay = yaml.safe_load(
            (ROOT / "compose.override.yaml").read_text(encoding="utf-8")
        )
        services = overlay["services"]
        self.assertEqual(services["ops-runner"]["profiles"], ["python-ops"])
        self.assertIn("read-ops-runner", services)
        change = services["change-ops-runner"]
        self.assertEqual(change["build"]["dockerfile"], "Dockerfile.ops-change")
        self.assertIn("node/apps/change-ops-runner/src/main.ts", change["command"])
        self.assertTrue(change["read_only"])
        volumes = "\n".join(str(item) for item in change["volumes"])
        for required in (
            "./local/servers.yaml:/agenelf/local/servers.yaml:ro",
            "./local/secrets:/agenelf/local/secrets:ro",
            "./data/ops-requests:/agenelf/data/ops-requests:ro",
            "./data/auth-decisions:/agenelf/data/auth-decisions:ro",
            "./data/ops-results:/agenelf/data/ops-results:rw",
            "./data/ops-locks:/agenelf/data/ops-locks:rw",
            "./data/ops-events:/agenelf/data/ops-events:rw",
        ):
            self.assertIn(required, volumes)
        for forbidden in (
            "/agenelf/approval",
            "docker.sock",
            "local/memory",
            "local/self",
            "/agenelf/app-fork",
            "/agenelf/policy",
        ):
            self.assertNotIn(forbidden, volumes)

    def test_python_rollback_remains_complete_and_unpartitioned(self) -> None:
        rollback = yaml.safe_load(
            (ROOT / "docker-compose.python.yml").read_text(encoding="utf-8")
        )
        ops = rollback["services"]["ops-runner"]
        self.assertNotIn("profiles", ops)
        self.assertNotIn("AGENELF_OPS_RUNNER_MODE", ops.get("environment", {}))
        self.assertIn("scripts/ops_runner_entry.py", " ".join(ops["command"]))
        self.assertNotIn("change-ops-runner", rollback["services"])

    def test_change_ops_files_are_in_owner_authorized_scope(self) -> None:
        plan = authorized_upgrade.make_plan(
            "升级 Node change privileged Ops SSH runner 和 Dockerfile.ops-change",
            scopes=["node_runners", "node_build", "compose"],
        )
        allowed = plan["allowed_paths"]
        for path in (
            "node/apps/change-ops-runner/",
            "node/packages/core/src/change-ops.ts",
            "node/packages/core/src/open-ssh.ts",
            "node/packages/core/src/server-catalog.ts",
            "Dockerfile.ops-change",
            "compose.override.yaml",
        ):
            self.assertIn(path, allowed)
        self.assertEqual(
            authorized_upgrade.CHANGE_OPS_UPGRADE_POLICY_VERSION,
            "owner-authorized-change-ops-v1",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
