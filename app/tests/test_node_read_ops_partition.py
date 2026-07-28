from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import yaml

from core import authorized_upgrade


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ops_runner_entry import is_semantic_read_request, runner_accepts_request  # noqa: E402


class NodeReadOpsPartitionTest(unittest.TestCase):
    def test_partition_uses_semantic_operation_not_untrusted_risk(self) -> None:
        read_claiming_change = {
            "capability": "docker.operations",
            "operation": "get_docker_logs",
            "risk": "change",
        }
        change_claiming_read = {
            "capability": "server.operations",
            "operation": "apt_update",
            "risk": "read",
        }
        self.assertTrue(is_semantic_read_request(read_claiming_change))
        self.assertFalse(is_semantic_read_request(change_claiming_read))
        self.assertFalse(runner_accepts_request(read_claiming_change, "change-only"))
        self.assertTrue(runner_accepts_request(change_claiming_read, "change-only"))
        self.assertTrue(runner_accepts_request(read_claiming_change, "read-only"))
        self.assertFalse(runner_accepts_request(change_claiming_read, "read-only"))

    def test_python_rollback_default_still_accepts_every_operation(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(
                runner_accepts_request(
                    {"capability": "server.operations", "operation": "inspect"}
                )
            )
            self.assertTrue(
                runner_accepts_request(
                    {"capability": "server.operations", "operation": "apt_update"}
                )
            )

    def test_default_overlay_separates_runner_permissions(self) -> None:
        compose = yaml.safe_load(
            (ROOT / "compose.override.yaml").read_text(encoding="utf-8")
        )
        services = compose["services"]
        self.assertEqual(
            services["ops-runner"]["environment"]["AGENELF_OPS_RUNNER_MODE"],
            "change-only",
        )
        runner = services["read-ops-runner"]
        self.assertEqual(runner["build"]["dockerfile"], "Dockerfile.ops-read")
        self.assertTrue(runner["read_only"])
        volumes = "\n".join(str(item) for item in runner["volumes"])
        for required in (
            "./local/servers.yaml:/agenelf/local/servers.yaml:ro",
            "./local/secrets:/agenelf/local/secrets:ro",
            "./data/ops-requests:/agenelf/data/ops-requests:ro",
            "./data/ops-results:/agenelf/data/ops-results:rw",
            "./data/ops-locks:/agenelf/data/ops-locks:rw",
            "./data/ops-events:/agenelf/data/ops-events:rw",
        ):
            self.assertIn(required, volumes)
        for forbidden in ("/agenelf/approval", "docker.sock", "local/memory", "local/self"):
            self.assertNotIn(forbidden, volumes)

    def test_python_rollback_compose_does_not_enable_partition(self) -> None:
        rollback = yaml.safe_load(
            (ROOT / "docker-compose.python.yml").read_text(encoding="utf-8")
        )
        environment = rollback["services"]["ops-runner"].get("environment", {})
        self.assertNotIn("AGENELF_OPS_RUNNER_MODE", environment)

    def test_owner_authorized_upgrade_scope_includes_read_ops_runtime(self) -> None:
        plan = authorized_upgrade.make_plan(
            "完善 Node read-only Ops SSH runner 与 Dockerfile.ops-read",
            scopes=["node_runners", "node_build"],
        )
        for expected in (
            "node/apps/read-ops-runner/",
            "node/packages/core/src/read-ops.ts",
            "node/packages/core/src/server-catalog.ts",
            "Dockerfile.ops-read",
            "node/tests/",
            "app/tests/",
        ):
            self.assertIn(expected, plan["allowed_paths"])
        self.assertEqual(
            authorized_upgrade.READ_OPS_UPGRADE_POLICY_VERSION,
            "owner-authorized-read-ops-v1",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
