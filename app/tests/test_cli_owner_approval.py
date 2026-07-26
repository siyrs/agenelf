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
            "kind": "operation",
            "operation": "compose_deploy",
            "target": "pve-ubuntu",
            "summary": "map 10808:1080",
        }

    def test_explicit_text_uses_broker_and_auto_continues(self):
        with (
            patch.object(
                cli_approval.approval_catalog,
                "resolve_pending_request",
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

    def test_upgrade_auth_approval_advances_deterministically_without_model_rediscovery(self):
        selected = {
            "id": "auth-0123456789ab",
            "kind": "authorization",
            "operation": "authorize_upgrade_intent",
            "target": "upgrade-test",
            "summary": "upgrade runner",
        }
        with (
            patch.object(
                cli_approval.approval_catalog,
                "resolve_pending_request",
                return_value=(selected, []),
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
                    "decision": {"decision": "approve"},
                },
            ),
            patch.object(
                cli_approval,
                "_advance_upgrade_after_approval",
                return_value={
                    "id": "upgrade-20260726-120000-12345678",
                    "status": "awaiting_candidate_approval",
                    "candidate_auth_id": "auth-candidate1234",
                },
            ) as advance,
        ):
            handled = cli_approval.handle_owner_decision(
                agent=self.agent,
                raw_input="/approve auth-0123456789ab",
                console=self.console,
                config={"cli": {"approval_auto_continue": True}},
            )
        self.assertTrue(handled)
        advance.assert_called_once()
        self.assertEqual(
            advance.call_args.kwargs["request_id"],
            "auth-0123456789ab",
        )
        self.assertEqual(
            self.agent.calls,
            [],
            "candidate approval must be shown directly, not rediscovered by the model",
        )

    def test_successful_upgrade_can_continue_original_task_once(self):
        selected = {
            "id": "auth-candidate1234",
            "kind": "authorization",
            "operation": "approve_tested_candidate",
            "target": "upgrade-test",
            "summary": "exact candidate",
        }
        with (
            patch.object(
                cli_approval.approval_catalog,
                "resolve_pending_request",
                return_value=(selected, []),
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
                    "decision": {"decision": "approve"},
                },
            ),
            patch.object(
                cli_approval,
                "_advance_upgrade_after_approval",
                return_value={
                    "id": "upgrade-20260726-120000-12345678",
                    "status": "succeeded",
                },
            ),
        ):
            handled = cli_approval.handle_owner_decision(
                agent=self.agent,
                raw_input="/approve auth-candidate1234",
                console=self.console,
                config={"cli": {"approval_auto_continue": True}},
            )
        self.assertTrue(handled)
        self.assertEqual(len(self.agent.calls), 1)
        self.assertIn("成功应用", self.agent.calls[0][0])

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
                cli_approval.approval_catalog,
                "resolve_pending_request",
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
