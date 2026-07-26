"""统一运行时策略引擎测试：真实策略加载、evaluate 规则与 empty 降级模式。"""

from __future__ import annotations

import unittest
from pathlib import Path

from core.policy import PolicyEngine

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_POLICY_VERSION = "1.1.0"


class PolicyEngineLoadingTest(unittest.TestCase):
    def test_real_policy_loads_successfully(self):
        engine = PolicyEngine(PROJECT_ROOT / "policy")
        self.assertFalse(engine.degraded)
        self.assertEqual(engine.policy_version, EXPECTED_POLICY_VERSION)

    def test_default_probe_uses_repo_root(self):
        engine = PolicyEngine()
        self.assertFalse(engine.degraded)
        self.assertEqual(engine.policy_dir, PROJECT_ROOT / "policy")


class PolicyEngineEvaluateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = PolicyEngine(PROJECT_ROOT / "policy")

    def test_read_operation_auto_executes_without_approval(self):
        result = self.engine.evaluate("server", "inspect_server")
        self.assertTrue(result["allowed"])
        self.assertEqual(result["risk"], "read")
        self.assertEqual(result["approval"], "none")
        self.assertTrue(result["auto_execute"])
        self.assertEqual(result["policy_version"], EXPECTED_POLICY_VERSION)

    def test_change_operation_requires_owner_exact(self):
        result = self.engine.evaluate("server", "apt_update")
        self.assertTrue(result["allowed"])
        self.assertEqual(result["risk"], "change")
        self.assertEqual(result["approval"], "owner_exact")
        self.assertFalse(result["auto_execute"])
        self.assertTrue(result["rollback_required"])

    def test_privileged_operation_requires_owner_elevated(self):
        result = self.engine.evaluate("docker", "docker_install")
        self.assertTrue(result["allowed"])
        self.assertEqual(result["risk"], "privileged")
        self.assertEqual(result["approval"], "owner_elevated")

    def test_irreversible_operation_requires_second_confirmation(self):
        result = self.engine.evaluate("database", "database_drop")
        self.assertTrue(result["allowed"])
        self.assertEqual(result["risk"], "irreversible")
        self.assertEqual(result["approval"], "owner_irreversible")
        self.assertTrue(result["second_confirmation_required"])
        # irreversible 策略用 backup_required_when_possible 而非 rollback_required
        self.assertFalse(result["rollback_required"])

    def test_forbidden_example_is_impossible_to_approve(self):
        result = self.engine.evaluate("agent", "self_approval")
        self.assertFalse(result["allowed"])
        self.assertEqual(result["risk"], "forbidden")
        self.assertEqual(result["approval"], "impossible")

    def test_named_forbidden_behavior_is_always_denied(self):
        result = self.engine.evaluate("agent", "self_approve_or_forge_owner_decision")
        self.assertFalse(result["allowed"])
        self.assertEqual(result["risk"], "forbidden")
        self.assertEqual(result["approval"], "impossible")

    def test_unknown_operation_defaults_to_deny(self):
        result = self.engine.evaluate("server", "rm_rf_everything")
        self.assertFalse(result["allowed"])
        self.assertEqual(result["risk"], "forbidden")
        self.assertEqual(result["approval"], "impossible")
        self.assertIn("默认拒绝", result["reason"])

    def test_unopened_channel_subject_is_denied(self):
        result = self.engine.evaluate("server", "inspect_server", subject="smart_fridge")
        self.assertFalse(result["allowed"])
        self.assertIn("渠道未开通", result["reason"])

    def test_mobile_device_read_passes_without_textual_confirmation(self):
        result = self.engine.evaluate("server", "inspect_server", subject="mobile_device")
        self.assertTrue(result["allowed"])
        self.assertEqual(result["risk"], "read")
        self.assertFalse(result["requires_textual_confirmation"])

    def test_mobile_device_change_requires_textual_confirmation(self):
        result = self.engine.evaluate("server", "apt_update", subject="mobile_device")
        self.assertTrue(result["allowed"])
        self.assertTrue(result["requires_textual_confirmation"])
        self.assertIn("不能充当批准人", result["reason"])


class PolicyEngineQueryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = PolicyEngine(PROJECT_ROOT / "policy")

    def test_is_protected_path_prefix_matching(self):
        self.assertTrue(self.engine.is_protected_path("policy/safety-constraints.v1.yaml"))
        self.assertTrue(self.engine.is_protected_path("scripts/validate_governance.py"))
        self.assertTrue(self.engine.is_protected_path("app/core/permissions.py"))
        self.assertFalse(self.engine.is_protected_path("app/skills/foo.py"))

    def test_candidate_limits_content(self):
        limits = self.engine.candidate_limits()
        self.assertEqual(limits["max_files"], 10)
        self.assertEqual(limits["max_changed_lines"], 500)
        self.assertTrue(limits["tests_required"])
        self.assertTrue(limits["full_suite_required"])
        self.assertTrue(limits["immutable_digest_required"])

    def test_acceptance_gates_content(self):
        gates = self.engine.acceptance_gates()
        for expected in (
            "policy_schema_valid",
            "full_unit_suite_passed",
            "exact_authorization_binding_verified",
            "trusted_evidence_archived",
            "documentation_updated",
        ):
            self.assertIn(expected, gates)

    def test_approval_requirements_by_mode(self):
        self.assertEqual(
            self.engine.approval_requirements("owner_exact"),
            ["exact_payload_fingerprint", "expiration", "single_use"],
        )
        self.assertIn("second_confirmation", self.engine.approval_requirements("owner_irreversible"))
        self.assertEqual(self.engine.approval_requirements("nonexistent_mode"), [])

    def test_forbidden_behaviors_content(self):
        behaviors = self.engine.forbidden_behaviors()
        self.assertIn("self_approve_or_forge_owner_decision", behaviors)
        self.assertIn("disable_or_bypass_policy_engine", behaviors)


class PolicyEngineEmptyModeTest(unittest.TestCase):
    def test_missing_dir_enters_degraded_mode_without_crash(self):
        engine = PolicyEngine(PROJECT_ROOT / "policy" / "does-not-exist")
        self.assertTrue(engine.degraded)
        self.assertEqual(engine.policy_version, "0.0.0-empty")
        result = engine.evaluate("server", "inspect_server")
        self.assertFalse(result["allowed"])
        self.assertEqual(result["risk"], "forbidden")
        self.assertEqual(result["approval"], "impossible")
        self.assertIn("降级模式", result["reason"])
        self.assertEqual(engine.candidate_limits(), {})
        self.assertEqual(engine.acceptance_gates(), [])
        self.assertEqual(engine.forbidden_behaviors(), [])
        self.assertFalse(engine.is_protected_path("policy/x.yaml"))


if __name__ == "__main__":
    unittest.main()
