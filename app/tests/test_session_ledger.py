from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.session_ledger import (
    MAX_PAYLOAD_BYTES,
    SessionLedgerError,
    SessionLedgerStore,
)


class SessionLedgerStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agenelf-ledger-test-")
        self.root = Path(self.temp.name)
        self.store = SessionLedgerStore(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_append_builds_tree_and_hash_chain(self) -> None:
        first = self.store.append(
            "demo-session",
            "message",
            {"role": "user", "content": "hello"},
        )
        second = self.store.append(
            "demo-session",
            "message",
            {"role": "assistant", "content": "hi"},
        )

        self.assertEqual(first["seq"], 1)
        self.assertIsNone(first["parent_id"])
        self.assertEqual(first["branch_id"], "main")
        self.assertEqual(second["seq"], 2)
        self.assertEqual(second["parent_id"], first["id"])
        self.assertEqual(second["prev_hash"], first["entry_hash"])
        self.assertEqual(self.store.verify("demo-session")["integrity"], "ok")

    def test_create_branch_keeps_append_order_and_tree_parent(self) -> None:
        root = self.store.append("branch-demo", "message", {"content": "root"})
        self.store.append("branch-demo", "message", {"content": "main child"})
        branch = self.store.create_branch(
            "branch-demo",
            root["id"],
            label="alternative",
            summary="try another route",
        )

        self.assertTrue(branch["branch_id"].startswith("br-"))
        self.assertEqual(branch["parent_id"], root["id"])
        self.assertEqual(branch["type"], "branch_summary")
        verification = self.store.verify("branch-demo")
        self.assertEqual(verification["integrity"], "ok")
        self.assertIn("main", verification["branches"])
        self.assertIn(branch["branch_id"], verification["branches"])

    def test_payload_is_recursively_redacted(self) -> None:
        entry = self.store.append(
            "privacy-demo",
            "custom",
            {
                "password": "top-secret",
                "nested": {
                    "token": "raw-token",
                    "text": "Authorization: Bearer abcdefghijklmnop",
                },
            },
        )

        payload = entry["payload"]
        self.assertEqual(payload["password"], "[REDACTED]")
        self.assertEqual(payload["nested"]["token"], "[REDACTED]")
        self.assertIn("Bearer [REDACTED]", payload["nested"]["text"])
        self.assertIn("_privacy_warnings", payload)

    def test_filters_and_exact_get(self) -> None:
        first = self.store.append("filter-demo", "message", {"content": "one"})
        self.store.append("filter-demo", "tool_call", {"name": "inspect"})
        last = self.store.append("filter-demo", "message", {"content": "two"})

        messages = self.store.entries(
            "filter-demo",
            event_type="message",
            limit=10,
        )
        self.assertEqual([item["id"] for item in messages], [first["id"], last["id"]])
        self.assertEqual(
            self.store.get("filter-demo", first["id"])["payload"]["content"],
            "one",
        )

    def test_tampering_is_detected(self) -> None:
        self.store.append("tamper-demo", "message", {"content": "original"})
        path = (
            self.root
            / "local"
            / "memory"
            / "session-ledger"
            / "tamper-demo.jsonl"
        )
        row = json.loads(path.read_text(encoding="utf-8").strip())
        row["payload"]["content"] = "changed"
        path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")

        verification = self.store.verify("tamper-demo")
        self.assertEqual(verification["integrity"], "failed")
        self.assertTrue(
            any("entry_hash" in error for error in verification["errors"]),
            verification,
        )

    def test_missing_parent_is_rejected(self) -> None:
        with self.assertRaises(SessionLedgerError):
            self.store.append(
                "parent-demo",
                "message",
                {"content": "child"},
                parent_id="evt-0000000000000000",
            )

    def test_invalid_session_and_oversized_payload_are_rejected(self) -> None:
        with self.assertRaises(SessionLedgerError):
            self.store.status("../escape")
        with self.assertRaises(SessionLedgerError):
            self.store.append(
                "large-demo",
                "custom",
                {"value": "x" * (MAX_PAYLOAD_BYTES + 1)},
            )

    def test_language_neutral_schema_stays_in_sync(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        schema = json.loads(
            (repo_root / "contracts" / "session-ledger-entry.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(schema["properties"]["schema_version"]["const"], 1)
        self.assertEqual(
            set(schema["properties"]["type"]["enum"]),
            {
                "message",
                "tool_call",
                "tool_result",
                "checkpoint",
                "reflection",
                "intention",
                "approval_ref",
                "evidence_ref",
                "branch_summary",
                "compaction",
                "label",
                "custom",
            },
        )


if __name__ == "__main__":
    unittest.main()
