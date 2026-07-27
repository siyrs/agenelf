from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
