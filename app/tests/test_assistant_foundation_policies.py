from __future__ import annotations

import unittest
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class AssistantFoundationPolicyTest(unittest.TestCase):
    def _load(self, name: str):
        return yaml.safe_load((PROJECT_ROOT / "policy" / name).read_text(encoding="utf-8"))

    def test_task_policy_requires_evidence_revision_and_shared_channels(self):
        policy = self._load("task-engine-constraints.v1.yaml")
        self.assertEqual(policy["schema_version"], 2)
        self.assertEqual(policy["completion_gate"]["minimum_trusted_evidence"], 1)
        self.assertIn("optimistic_concurrency", {item["id"] for item in policy["principles"]})
        self.assertEqual(
            set(policy["shared_channel_contract"]["channels"]),
            {"cli", "http", "web", "mobile", "voice"},
        )
        self.assertTrue(policy["shared_channel_contract"]["mobile_or_voice_bypass_forbidden"])

    def test_model_policy_forbids_credentials_and_model_authorization(self):
        policy = self._load("model-routing-constraints.v1.yaml")
        self.assertIn("return_api_key_or_token", policy["forbidden"])
        self.assertIn("treat_model_output_as_owner_approval", policy["forbidden"])
        self.assertIn("privacy_route", {item["id"] for item in policy["principles"]})

    def test_channel_policy_has_replay_and_reference_only_authorization(self):
        policy = self._load("channel-constraints.v1.yaml")
        ids = {item["id"] for item in policy["principles"]}
        self.assertIn("replay_protection", ids)
        self.assertIn("reference_only_authorization", ids)
        self.assertIn("voice_authorization_by_transcript_only", policy["forbidden"])
        self.assertNotIn("bearer_token", policy["safe_metadata"])

    def test_docs_are_honest_about_unfinished_clients(self):
        channels = (PROJECT_ROOT / "docs" / "CHANNELS.md").read_text(encoding="utf-8")
        tasks = (PROJECT_ROOT / "docs" / "TASK_ENGINE.md").read_text(encoding="utf-8")
        self.assertIn("尚未宣称手机 APP", channels)
        self.assertIn("尚未宣称完成", tasks)
        self.assertIn("不能独立构成不可逆授权", channels)


if __name__ == "__main__":
    unittest.main(verbosity=2)
