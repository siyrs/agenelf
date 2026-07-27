from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from core import operations
from core.execution_policy import resolve_contract
from skills import operation_control


class OperationControlSkillTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_root = os.environ.get("AGENELF_ROOT")
        os.environ["AGENELF_ROOT"] = str(self.root)

    def tearDown(self) -> None:
        if self.old_root is None:
            os.environ.pop("AGENELF_ROOT", None)
        else:
            os.environ["AGENELF_ROOT"] = self.old_root
        self.tmp.cleanup()

    def _request(self) -> dict:
        return operations.submit_operation(
            "server.operations",
            "apt_update",
            "primary",
            {},
            operations.RISK_CHANGE,
            "更新 APT",
            root=self.root,
            deduplicate=False,
        )

    def test_tools_are_read_only_and_no_revoke_function_is_exposed(self) -> None:
        names = [item["function"]["name"] for item in operation_control.TOOLS]
        self.assertEqual(
            names,
            [
                "list_revocable_operations",
                "get_operation_control_status",
                "get_operation_revocation_instructions",
            ],
        )
        self.assertNotIn("revoke_operation", names)
        for name in names:
            contract = resolve_contract(name, {}, operation_control)
            self.assertIsNotNone(contract)
            self.assertEqual(contract.risk, "read")
            self.assertEqual(contract.execution_mode, "pure")

    def test_model_gets_owner_commands_but_cannot_execute_revocation(self) -> None:
        request = self._request()
        value = json.loads(
            operation_control.execute(
                "get_operation_revocation_instructions",
                {"operation_id": request["id"]},
            )
        )
        self.assertEqual(value["status"], "owner_action_required")
        instructions = value["instructions"]
        self.assertIn("revoke.ps1", instructions)
        self.assertIn("scripts/revoke.py", instructions)
        self.assertIn("revoke.sh", instructions)
        self.assertFalse(
            (self.root / "data" / "ops-results" / f"{request['id']}.json").exists()
        )

    def test_completed_request_has_no_revocation_instructions(self) -> None:
        request = self._request()
        path = self.root / "data" / "ops-results" / f"{request['id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"id": request["id"], "status": "succeeded"}),
            encoding="utf-8",
        )

        value = json.loads(
            operation_control.execute(
                "get_operation_revocation_instructions",
                {"operation_id": request["id"]},
            )
        )
        self.assertEqual(value["status"], "not_revocable")
        self.assertNotIn("instructions", value)


if __name__ == "__main__":
    unittest.main(verbosity=2)
