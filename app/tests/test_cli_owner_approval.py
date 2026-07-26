from __future__ import annotations

import unittest
from unittest.mock import patch

from rich.console import Console

from core import cli_approval


class FakeAgent:
    def __init__(self):
        self.calls = []

    def chat(self, message, subject="agent"):
        self.calls.append((message, subject))
        return "已继续并验证"


class CliApprovalTest(unittest.TestCase):
    def setUp(self):
        self.console = Console(record=True, width=120)
        self.agent = FakeAgent()
        self.selected = {
            "id": "op-0123456789abcdef",
            "operation": "compose_deploy",
            "target": "pve-ubuntu",
            "summary": "map 10808:1080",
        }

    def test_explicit_text_uses_broker_and_auto_continues(self):
        with (
            patch.object(
                cli_approval.owner_approval,
                "resolve_pending_operation",
                return_value=(self.selected, []),
            ),
            patch.object(
                cli_approval.owner_approval,
                "submit_owner_command",
                return_value={"id": "owner-decision-0123456789abcdef"},
            ),
            patch.object(
                cli_approval.owner_approval,
                "wait_for_command_result",
                return_value={
                    "status": "succeeded",
                    "decision": {
                        "decision": "approve",
                        "superseded_duplicates": [],
                    },
                },
            ),
            patch.object(
                cli_approval.operations,
                "wait_for_result",
                return_value={"status": "approved"},
            ),
        ):
            handled = cli_approval.handle_owner_decision(
                agent=self.agent,
                raw_input="审批通过 op-0123456789abcdef",
                console=self.console,
                config={"cli": {"approval_auto_continue": True}},
            )
        self.assertTrue(handled)
        self.assertEqual(len(self.agent.calls), 1)
        self.assertIn("op-0123456789abcdef", self.agent.calls[0][0])
        self.assertEqual(self.agent.calls[0][1], "cli")

    def test_ordinary_prose_is_not_intercepted(self):
        handled = cli_approval.handle_owner_decision(
            agent=self.agent,
            raw_input="我觉得这个方案可以批准，但先分析风险",
            console=self.console,
            config={},
        )
        self.assertFalse(handled)
        self.assertEqual(self.agent.calls, [])

    def test_deny_does_not_auto_continue(self):
        with (
            patch.object(
                cli_approval.owner_approval,
                "resolve_pending_operation",
                return_value=(self.selected, []),
            ),
            patch.object(
                cli_approval.owner_approval,
                "submit_owner_command",
                return_value={"id": "owner-decision-0123456789abcdef"},
            ),
            patch.object(
                cli_approval.owner_approval,
                "wait_for_command_result",
                return_value={
                    "status": "succeeded",
                    "decision": {"decision": "deny"},
                },
            ),
        ):
            handled = cli_approval.handle_owner_decision(
                agent=self.agent,
                raw_input="/deny op-0123456789abcdef 暂不修改",
                console=self.console,
                config={},
            )
        self.assertTrue(handled)
        self.assertEqual(self.agent.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
