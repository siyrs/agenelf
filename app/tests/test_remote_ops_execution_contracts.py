from __future__ import annotations

import unittest

from core.execution_policy import resolve_contract
from skills import docker_ops, task_continuation


class RemoteOpsExecutionContractsTest(unittest.TestCase):
    def test_remote_docker_reads_use_queued_runner(self) -> None:
        for tool in (
            "get_docker_logs",
            "inspect_docker_container",
            "run_docker_check",
        ):
            with self.subTest(tool=tool):
                contract = resolve_contract(tool, {}, docker_ops)
                self.assertIsNotNone(contract)
                self.assertEqual(contract.capability, "docker.operations")
                self.assertEqual(contract.risk, "read")
                self.assertEqual(contract.execution_mode, "queued_runner")

    def test_remote_docker_restart_is_change_on_queued_runner(self) -> None:
        contract = resolve_contract(
            "restart_docker_container",
            {"target": "primary", "container": "sing-box"},
            docker_ops,
        )
        self.assertIsNotNone(contract)
        self.assertEqual(contract.risk, "change")
        self.assertEqual(contract.execution_mode, "queued_runner")

    def test_runtime_catalog_and_result_lookup_are_pure(self) -> None:
        for tool in ("list_docker_runtime", "get_docker_operation"):
            contract = resolve_contract(tool, {}, docker_ops)
            self.assertIsNotNone(contract)
            self.assertEqual(contract.execution_mode, "pure")

    def test_task_continuation_mutations_are_local_state_only(self) -> None:
        for tool in (
            "checkpoint_task_continuation",
            "complete_task_continuation",
            "retry_task_continuation",
            "cancel_task_continuation",
        ):
            with self.subTest(tool=tool):
                contract = resolve_contract(tool, {}, task_continuation)
                self.assertIsNotNone(contract)
                self.assertEqual(contract.capability, "agent.task_continuation")
                self.assertEqual(contract.risk, "change")
                self.assertEqual(contract.execution_mode, "local_state")


if __name__ == "__main__":
    unittest.main(verbosity=2)
