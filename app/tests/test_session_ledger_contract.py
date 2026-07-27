from __future__ import annotations

import json
import unittest

from core.execution_policy import resolve_contract
from skills import session_ledger as session_ledger_skill


class SessionLedgerContractTest(unittest.TestCase):
    def test_skill_operations_have_explicit_execution_contracts(self) -> None:
        append_contract = resolve_contract(
            "session_ledger_append",
            {},
            session_ledger_skill,
        )
        status_contract = resolve_contract(
            "session_ledger_status",
            {},
            session_ledger_skill,
        )

        self.assertIsNotNone(append_contract)
        self.assertEqual(append_contract.risk, "change")
        self.assertEqual(append_contract.execution_mode, "local_state")
        self.assertIsNotNone(status_contract)
        self.assertEqual(status_contract.risk, "read")
        self.assertEqual(status_contract.execution_mode, "pure")

    def test_model_tool_cannot_claim_runtime_or_trusted_evidence_events(self) -> None:
        append_tool = next(
            item
            for item in session_ledger_skill.TOOLS
            if item["function"]["name"] == "session_ledger_append"
        )
        event_types = set(
            append_tool["function"]["parameters"]["properties"]["event_type"]["enum"]
        )
        self.assertEqual(
            event_types,
            {"message", "checkpoint", "reflection", "intention", "label", "custom"},
        )
        self.assertTrue(
            event_types.isdisjoint(
                {
                    "tool_call",
                    "tool_result",
                    "approval_ref",
                    "evidence_ref",
                    "compaction",
                    "branch_summary",
                }
            )
        )

        result = json.loads(
            session_ledger_skill.execute(
                "session_ledger_append",
                {
                    "session_id": "trust-test",
                    "event_type": "evidence_ref",
                    "payload": {"reference": "fake"},
                },
            )
        )
        self.assertIn("不能写入安全关键事件类型", result["error"])


if __name__ == "__main__":
    unittest.main()
