from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from core.agent_events import (
    EVENT_TYPES,
    ORIGINS,
    AgentEventError,
    AgentEventHub,
    EventCursorExpired,
    RunAlreadyTerminal,
    RunEventStream,
    validate_event_envelope,
)
from core.session_ledger import SessionLedgerStore


class AgentEventCoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agenelf-events-test-")
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_sequence_replay_terminal_and_ledger_persistence(self) -> None:
        stream = RunEventStream(
            root=self.root,
            session_id="event-demo",
            run_id="run-0000000000000001",
        )
        started = stream.emit("run.started", {"source": "test"})
        delta = stream.emit("message.delta", {"delta": "hel"})
        completed = stream.emit("message.completed", {"text": "hello"})
        settled = stream.emit("run.settled", {"reason": "completed"})

        self.assertEqual([started.seq, delta.seq, completed.seq, settled.seq], [1, 2, 3, 4])
        self.assertFalse(started.transient)
        self.assertTrue(delta.transient)
        self.assertEqual(stream.terminal_type, "run.settled")
        self.assertTrue(stream.is_terminal)
        self.assertEqual(
            [item["type"] for item in stream.events_after(0, limit=20)],
            ["run.started", "message.delta", "message.completed", "run.settled"],
        )
        self.assertEqual(stream.wait_after(4, timeout_seconds=0.01), [])

        ledger = SessionLedgerStore(self.root)
        persisted = ledger.entries("event-demo", limit=20)
        self.assertEqual(len(persisted), 3, "transient delta must not be persisted")
        self.assertEqual(
            [item["payload"]["agent_event"]["type"] for item in persisted],
            ["run.started", "message.completed", "run.settled"],
        )
        self.assertTrue(all(item["origin"] == "runtime" for item in persisted))
        self.assertEqual(ledger.verify("event-demo")["integrity"], "ok")

    def test_payload_is_redacted_before_publish_and_persist(self) -> None:
        stream = RunEventStream(root=self.root, session_id="privacy-events")
        event = stream.emit(
            "run.started",
            {
                "password": "secret-value",
                "nested": {
                    "token": "raw-token",
                    "header": "Authorization: Bearer abcdefghijklmnop",
                },
            },
        )
        self.assertEqual(event.payload["password"], "[REDACTED]")
        self.assertEqual(event.payload["nested"]["token"], "[REDACTED]")
        self.assertIn("Bearer [REDACTED]", event.payload["nested"]["header"])
        self.assertIn("_privacy_warnings", event.payload)

        persisted = SessionLedgerStore(self.root).entries("privacy-events", limit=5)
        self.assertEqual(persisted[0]["payload"]["agent_event"]["payload"], event.payload)

    def test_terminal_event_is_exactly_once(self) -> None:
        stream = RunEventStream(root=self.root, session_id="terminal-demo")
        stream.emit("run.started")
        stream.emit("run.failed", {"error": "deterministic failure"})
        with self.assertRaises(RunAlreadyTerminal):
            stream.emit("run.settled")
        with self.assertRaises(RunAlreadyTerminal):
            stream.emit("message.completed", {"text": "too late"})

    def test_cursor_expiry_is_explicit(self) -> None:
        stream = RunEventStream(
            root=self.root,
            session_id="cursor-demo",
            max_buffer_events=3,
            persist_durable_events=False,
        )
        stream.emit("run.started")
        for index in range(4):
            stream.emit("message.delta", {"delta": str(index)})
        self.assertEqual(stream.snapshot()["oldest_buffered_seq"], 3)
        with self.assertRaises(EventCursorExpired):
            stream.events_after(0)
        self.assertEqual(
            [item["seq"] for item in stream.events_after(2)],
            [3, 4, 5],
        )

    def test_wait_after_wakes_on_new_event(self) -> None:
        stream = RunEventStream(
            root=self.root,
            session_id="wait-demo",
            persist_durable_events=False,
        )

        def emit_later() -> None:
            time.sleep(0.05)
            stream.emit("run.started", {"ready": True})

        worker = threading.Thread(target=emit_later)
        worker.start()
        values = stream.wait_after(0, timeout_seconds=2)
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual([item["type"] for item in values], ["run.started"])

    def test_concurrent_emitters_preserve_single_run_sequence(self) -> None:
        stream = RunEventStream(
            root=self.root,
            session_id="thread-demo",
            max_buffer_events=200,
            persist_durable_events=False,
        )
        stream.emit("run.started")

        def emit_many(worker_id: int) -> None:
            for index in range(25):
                stream.emit(
                    "tool.delta",
                    {"worker": worker_id, "index": index},
                )

        workers = [threading.Thread(target=emit_many, args=(worker,)) for worker in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())

        stream.emit("run.settled")
        values = stream.events_after(0, limit=500)
        self.assertEqual(len(values), 102)
        self.assertEqual([item["seq"] for item in values], list(range(1, 103)))
        self.assertEqual(len({item["id"] for item in values}), 102)

    def test_hub_evicts_only_terminal_runs(self) -> None:
        hub = AgentEventHub(root=self.root, max_runs=2, max_buffer_events=20)
        first = hub.create("hub-one", run_id="run-0000000000000001", persist_durable_events=False)
        first.emit("run.started")
        first.emit("run.settled")
        second = hub.create("hub-two", run_id="run-0000000000000002", persist_durable_events=False)
        second.emit("run.started")
        third = hub.create("hub-three", run_id="run-0000000000000003", persist_durable_events=False)
        third.emit("run.started")

        with self.assertRaises(AgentEventError):
            hub.get(first.run_id)
        self.assertEqual({item["run_id"] for item in hub.list_runs()}, {second.run_id, third.run_id})
        with self.assertRaises(AgentEventError):
            hub.remove(second.run_id)
        second.emit("run.cancelled")
        self.assertTrue(hub.remove(second.run_id))

    def test_invalid_inputs_and_import_validation(self) -> None:
        with self.assertRaises(AgentEventError):
            RunEventStream(root=self.root, session_id="../escape")
        stream = RunEventStream(root=self.root, session_id="validation-demo")
        with self.assertRaises(AgentEventError):
            stream.emit("invented.event")
        with self.assertRaises(AgentEventError):
            stream.emit("run.started", origin="trusted-because-model-said-so")
        with self.assertRaises(AgentEventError):
            stream.emit("run.started", {"value": "x" * (70 * 1024)})

        event = stream.emit("run.started", {"ok": True}).to_dict()
        self.assertEqual(validate_event_envelope(event), event)
        corrupted = {**event, "id": "bad"}
        with self.assertRaises(AgentEventError):
            validate_event_envelope(corrupted)

    def test_schema_enums_stay_in_sync(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        schema = json.loads(
            (repo_root / "contracts" / "agent-event-envelope.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(schema["properties"]["schema_version"]["const"], 1)
        self.assertEqual(set(schema["properties"]["type"]["enum"]), EVENT_TYPES)
        self.assertEqual(set(schema["properties"]["origin"]["enum"]), ORIGINS)
        self.assertEqual(
            set(schema["required"]),
            {
                "schema_version",
                "id",
                "session_id",
                "run_id",
                "seq",
                "type",
                "origin",
                "ts",
                "transient",
                "payload",
            },
        )


if __name__ == "__main__":
    unittest.main()
