from __future__ import annotations

import copy
import importlib.util
import unittest
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "agenelf_governance_validator",
    PROJECT_ROOT / "scripts" / "validate_governance.py",
)
validator = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(validator)


class GovernancePolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy_path = PROJECT_ROOT / "policy" / "safety-constraints.v1.yaml"
        cls.policy = yaml.safe_load(cls.policy_path.read_text(encoding="utf-8"))

    def test_repository_policy_passes_validator(self):
        self.assertEqual(validator.validate_policy(self.policy), [])

    def test_owner_can_authorize_dangerous_but_not_forbidden_actions(self):
        risks = self.policy["risk_levels"]
        self.assertEqual(risks["privileged"]["approval"], "owner_elevated")
        self.assertEqual(risks["irreversible"]["approval"], "owner_irreversible")
        self.assertEqual(risks["forbidden"]["approval"], "impossible")
        self.assertTrue(
            self.policy["owner_authorization"]["never_overrides_forbidden"]
        )

    def test_authorization_is_bound_to_exact_payload_and_single_use(self):
        fields = set(self.policy["owner_authorization"]["exact_binding_fields"])
        self.assertTrue(
            {
                "capability",
                "operation",
                "target",
                "canonical_parameters_hash",
                "risk",
                "nonce",
                "expires_at",
            }.issubset(fields)
        )
        for mode in self.policy["owner_authorization"]["modes"].values():
            requirements = set(mode["requirements"])
            self.assertIn("single_use", requirements)
            self.assertIn("exact_payload_fingerprint", requirements)

    def test_self_evolution_cannot_merge_main_or_weaken_gate(self):
        evolution = self.policy["self_evolution"]
        self.assertFalse(evolution["auto_pursue"])
        self.assertIn("autonomously_merge_main", evolution["forbidden"])
        self.assertIn(
            "weaken_tests_or_gate_to_make_a_candidate_pass",
            self.policy["forbidden_behaviors"],
        )
        self.assertIn("policy/", self.policy["protected_paths"])
        self.assertIn("scripts/", self.policy["protected_paths"])

    def test_validator_rejects_weakened_policy(self):
        weakened = copy.deepcopy(self.policy)
        weakened["risk_levels"]["privileged"]["auto_execute"] = True
        weakened["owner_authorization"]["exact_binding_fields"].remove("nonce")
        weakened["self_evolution"]["forbidden"].remove("autonomously_merge_main")
        errors = validator.validate_policy(weakened)
        text = "\n".join(errors)
        self.assertIn("privileged.auto_execute", text)
        self.assertIn("nonce", text)
        self.assertIn("自主合并 main", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
