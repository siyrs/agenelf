from __future__ import annotations

import copy
import os
import unittest
from types import SimpleNamespace

from core import authorized_upgrade_recovery


class FakeUpgradeModule(SimpleNamespace):
    def __init__(self, *, fail_count: int = 0) -> None:
        self.state = {
            "id": "upgrade-20260726-120000-12345678",
            "status": "generation_failed",
            "goal": "upgrade runner",
            "intent_consumed": True,
            "generation_attempts": 0,
            "changed_file_records": [],
        }
        self.fail_count = fail_count
        self.generate_calls = 0
        self.intent_requests = 0
        self.candidate_requests = 0
        self._agenelf_authorized_upgrade_recovery_installed = False
        self._generate_candidate = self._original_generate
        self.advance_session = self._original_advance
        self._build_prompt = self._original_prompt
        self.public_status = self._original_public
        self.load_session = self._load
        self.save_session = self._save
        self._candidate_auth_state = lambda session: session.get(
            "candidate_auth_state", "pending"
        )
        self._intent_auth_state = lambda session: session.get(
            "intent_auth_state", "approved"
        )
        self._request_candidate_approval = self._request_candidate
        self._request_intent_approval = self._request_intent

    def _load(self, session_id: str):
        self.assert_session(session_id)
        return copy.deepcopy(self.state)

    def _save(self, session):
        self.state = copy.deepcopy(session)
        return copy.deepcopy(self.state)

    def assert_session(self, session_id: str) -> None:
        if session_id != self.state["id"]:
            raise ValueError(session_id)

    def _original_generate(self, agent, session):
        del agent
        self.generate_calls += 1
        if self.generate_calls <= self.fail_count:
            raise RuntimeError("temporary model failure")
        session = copy.deepcopy(session)
        session.update(
            {
                "status": "awaiting_candidate_approval",
                "candidate_auth_id": "auth-candidate1234",
                "candidate_digest": "c" * 64,
                "test_report_sha256": "d" * 64,
                "baseline_manifest_sha256": "e" * 64,
                "changed_files": ["app/core/example.py"],
                "changed_file_records": [
                    {
                        "path": "app/core/example.py",
                        "created": False,
                        "changed_lines": 4,
                        "before_sha256": "a" * 64,
                        "after_sha256": "b" * 64,
                    }
                ],
                "candidate_binding": {
                    "candidate_tree_sha256": "c" * 64,
                    "test_report_sha256": "d" * 64,
                    "baseline_manifest_sha256": "e" * 64,
                },
            }
        )
        self._save(session)
        return session

    def _original_advance(self, agent, session_id: str, *, wait_seconds: float = 2.0):
        del agent, wait_seconds
        return self._load(session_id)

    @staticmethod
    def _original_prompt(session, context):
        del session, context
        return "base prompt"

    @staticmethod
    def _original_public(session):
        return {
            "id": session.get("id"),
            "status": session.get("status"),
        }

    def _request_candidate(self, session, binding):
        self.candidate_requests += 1
        session["candidate_auth_id"] = f"auth-reissued{self.candidate_requests}"
        session["candidate_binding"] = dict(binding)
        return session["candidate_auth_id"]

    def _request_intent(self, session):
        self.intent_requests += 1
        session["intent_auth_id"] = f"auth-intent{self.intent_requests}"
        return session["intent_auth_id"]


class AuthorizedUpgradeRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.old_attempts = os.environ.get(
            "AGENELF_AUTHORIZED_UPGRADE_GENERATION_ATTEMPTS"
        )

    def tearDown(self) -> None:
        if self.old_attempts is None:
            os.environ.pop(
                "AGENELF_AUTHORIZED_UPGRADE_GENERATION_ATTEMPTS",
                None,
            )
        else:
            os.environ[
                "AGENELF_AUTHORIZED_UPGRADE_GENERATION_ATTEMPTS"
            ] = self.old_attempts

    @staticmethod
    def agent(limit: int = 3):
        return SimpleNamespace(
            config={
                "autonomy": {
                    "owner_authorized_upgrade": {
                        "max_generation_attempts": limit,
                    }
                }
            }
        )

    def test_transient_generation_failure_retries_without_new_intent_auth(self) -> None:
        module = FakeUpgradeModule(fail_count=1)
        authorized_upgrade_recovery.install(module)

        first = module.advance_session(self.agent(), module.state["id"])
        self.assertEqual(first["status"], "generation_failed")
        self.assertEqual(first["generation_attempts"], 1)
        self.assertIn("temporary model failure", first["last_generation_error"])
        self.assertEqual(module.intent_requests, 0)

        second = module.advance_session(self.agent(), module.state["id"])
        self.assertEqual(second["status"], "awaiting_candidate_approval")
        self.assertEqual(second["generation_attempts"], 2)
        self.assertEqual(module.generate_calls, 2)
        self.assertEqual(module.intent_requests, 0)

    def test_generation_retry_limit_stops_without_infinite_loop(self) -> None:
        module = FakeUpgradeModule(fail_count=99)
        authorized_upgrade_recovery.install(module)
        agent = self.agent(limit=2)

        self.assertEqual(
            module.advance_session(agent, module.state["id"])["status"],
            "generation_failed",
        )
        self.assertEqual(
            module.advance_session(agent, module.state["id"])["status"],
            "generation_failed",
        )
        final = module.advance_session(agent, module.state["id"])
        self.assertEqual(final["status"], "failed")
        self.assertIn("重试上限 2", final["error"])
        self.assertEqual(module.generate_calls, 2)

    def test_denied_exact_candidate_does_not_revoke_intent(self) -> None:
        module = FakeUpgradeModule()
        module.state.update(
            {
                "status": "awaiting_candidate_approval",
                "candidate_auth_state": "denied",
                "candidate_auth_id": "auth-old-candidate",
                "candidate_binding": {"candidate_tree_sha256": "c" * 64},
            }
        )
        authorized_upgrade_recovery.install(module)

        denied = module.advance_session(self.agent(), module.state["id"])
        self.assertEqual(denied["status"], "candidate_denied")
        self.assertTrue(denied["intent_consumed"])
        self.assertEqual(module.generate_calls, 0)

        replacement = module.advance_session(self.agent(), module.state["id"])
        self.assertEqual(replacement["status"], "awaiting_candidate_approval")
        self.assertEqual(module.generate_calls, 1)

    def test_invalid_candidate_authorization_is_reissued_for_same_digest(self) -> None:
        module = FakeUpgradeModule()
        module.state.update(
            {
                "status": "awaiting_candidate_approval",
                "candidate_auth_state": "invalid",
                "candidate_auth_id": "auth-expired",
                "candidate_binding": {"candidate_tree_sha256": "c" * 64},
            }
        )
        authorized_upgrade_recovery.install(module)
        value = module.advance_session(self.agent(), module.state["id"])
        self.assertEqual(value["status"], "awaiting_candidate_approval")
        self.assertEqual(value["candidate_auth_id"], "auth-reissued1")
        self.assertEqual(module.generate_calls, 0)

    def test_public_status_shows_exact_candidate_summary(self) -> None:
        module = FakeUpgradeModule()
        module._original_generate(None, module.state)
        authorized_upgrade_recovery.install(module)
        public = module.public_status(module.state)
        self.assertEqual(public["candidate_files"][0]["path"], "app/core/example.py")
        self.assertEqual(public["candidate_files"][0]["changed_lines"], 4)
        self.assertEqual(public["candidate_files"][0]["before_sha256"], "a" * 16)
        self.assertEqual(public["candidate_files"][0]["after_sha256"], "b" * 16)
        self.assertEqual(
            public["candidate_binding_summary"]["candidate_tree_sha256"],
            "c" * 64,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
