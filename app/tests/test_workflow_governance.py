import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class WorkflowGovernanceTest(unittest.TestCase):
    def test_workflow_policy_contains_core_safety_rules(self):
        policy = PROJECT_ROOT / "policy" / "workflow-constraints.v1.yaml"
        text = policy.read_text(encoding="utf-8")
        self.assertIn("no_hidden_side_effects", text)
        self.assertIn("evidence_plan", text)
        self.assertIn("direct_main_branch_mutation", text)

    def test_workflow_keeps_channels_on_same_control_plane(self):
        doc = PROJECT_ROOT / "docs" / "WORKFLOW_ORCHESTRATION.md"
        text = doc.read_text(encoding="utf-8")
        self.assertIn("CLI", text)
        self.assertIn("Voice", text)
        self.assertIn("禁止语音或移动端绕过权限系统", text)


if __name__ == "__main__":
    unittest.main()
