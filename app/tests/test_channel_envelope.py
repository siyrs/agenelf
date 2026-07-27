from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.channel_envelope import ChannelEnvelopeError, CommandEnvelopeStore


class ChannelEnvelopeTest(unittest.TestCase):
    def test_all_channels_share_one_envelope_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CommandEnvelopeStore(Path(tmp))
            for index, channel in enumerate(("cli", "http", "web", "mobile", "voice")):
                value = store.create(
                    channel=channel,
                    actor_id="owner-sirius",
                    session_id=f"session-{channel}",
                    message=f"检查服务器 {channel}",
                    idempotency_key=f"request-{index:08d}",
                    authorization_refs=["task-abcdef123456"],
                    metadata={"locale": "zh-CN", "client_version": "1.0"},
                )
                self.assertEqual(value["channel"], channel)
                self.assertTrue(value["authorization_is_reference_only"])
                self.assertEqual(value["schema_version"], 1)

    def test_same_idempotency_replays_same_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CommandEnvelopeStore(Path(tmp))
            kwargs = {
                "channel": "mobile",
                "actor_id": "owner",
                "session_id": "session-1",
                "message": "部署服务",
                "idempotency_key": "mobile-request-0001",
            }
            first = store.create(**kwargs)
            second = store.create(**kwargs)
            self.assertEqual(first["id"], second["id"])
            self.assertTrue(second["replayed"])
            self.assertEqual(len(list((Path(tmp) / "data" / "channel-requests").glob("cmd-*.json"))), 1)

    def test_reused_key_with_different_payload_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CommandEnvelopeStore(Path(tmp))
            store.create(
                channel="voice",
                actor_id="owner",
                session_id="voice-1",
                message="查看状态",
                idempotency_key="voice-command-0001",
            )
            with self.assertRaisesRegex(ChannelEnvelopeError, "不同载荷"):
                store.create(
                    channel="voice",
                    actor_id="owner",
                    session_id="voice-1",
                    message="删除数据",
                    idempotency_key="voice-command-0001",
                )

    def test_credentials_are_redacted_before_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CommandEnvelopeStore(Path(tmp))
            value = store.create(
                channel="http",
                actor_id="owner",
                session_id="s-1",
                message="使用 token=very-secret-token 运行检查",
                idempotency_key="http-command-0001",
                metadata={"source_ip": "127.0.0.1", "device_id": "phone-1"},
            )
            self.assertNotIn("very-secret-token", value["message"])
            self.assertTrue(value["credentials_redacted"])
            self.assertNotIn("source_ip", value["metadata"])
            saved = store.get(value["id"])
            self.assertNotIn("very-secret-token", str(saved))

    def test_authorization_must_be_reference_not_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CommandEnvelopeStore(Path(tmp))
            with self.assertRaisesRegex(ChannelEnvelopeError, "授权引用"):
                store.create(
                    channel="mobile",
                    actor_id="owner",
                    session_id="s-1",
                    message="执行任务",
                    idempotency_key="mobile-command-0001",
                    authorization_refs=["Bearer plaintext-token"],
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
