"""移动端/语音渠道确认规则测试（报告点名）：移动端绝不能直接批准高风险操作。"""

from __future__ import annotations

import unittest
from pathlib import Path

from core.policy import PolicyEngine

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class MobileConfirmationRulesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = PolicyEngine(PROJECT_ROOT / "policy")

    def test_mobile_read_passes_through(self):
        result = self.engine.evaluate("server", "inspect_server", subject="mobile_device")
        self.assertTrue(result["allowed"])
        self.assertEqual(result["risk"], "read")
        self.assertTrue(result["auto_execute"])
        self.assertFalse(result["requires_textual_confirmation"])

    def test_mobile_change_must_return_to_textual_confirmation(self):
        for subject in ("mobile_device", "voice"):
            result = self.engine.evaluate("server", "apt_update", subject=subject)
            self.assertTrue(result["allowed"], subject)
            self.assertEqual(result["approval"], "owner_exact", subject)
            self.assertTrue(result["requires_textual_confirmation"], subject)

    def test_mobile_privileged_requires_textual_and_second_confirmation(self):
        result = self.engine.evaluate("docker", "docker_install", subject="mobile_device")
        self.assertTrue(result["allowed"])
        self.assertEqual(result["approval"], "owner_elevated")
        self.assertTrue(result["requires_textual_confirmation"])
        self.assertTrue(result["second_confirmation_required"])

    def test_mobile_cannot_act_as_approver_is_explicitly_denied_in_reason(self):
        for subject in ("mobile_device", "voice"):
            result = self.engine.evaluate("server", "apt_update", subject=subject)
            self.assertIn("不能充当批准人", result["reason"], subject)

    def test_mobile_forbidden_operation_stays_forbidden(self):
        result = self.engine.evaluate("agent", "self_approval", subject="mobile_device")
        self.assertFalse(result["allowed"])
        self.assertEqual(result["approval"], "impossible")


class CliHttpChannelsUnaffectedTest(unittest.TestCase):
    """cli/http 已开通渠道的行为不受移动端规则影响。"""

    @classmethod
    def setUpClass(cls):
        cls.engine = PolicyEngine(PROJECT_ROOT / "policy")

    def test_cli_read_and_change_follow_standard_rules(self):
        read = self.engine.evaluate("server", "inspect_server", subject="cli")
        self.assertTrue(read["allowed"])
        self.assertTrue(read["auto_execute"])
        self.assertFalse(read["requires_textual_confirmation"])

        change = self.engine.evaluate("server", "apt_update", subject="cli")
        self.assertTrue(change["allowed"])
        self.assertFalse(change["requires_textual_confirmation"])
        self.assertNotIn("不能充当批准人", change["reason"])

    def test_http_privileged_keeps_single_confirmation(self):
        result = self.engine.evaluate("docker", "docker_install", subject="http")
        self.assertTrue(result["allowed"])
        self.assertFalse(result["requires_textual_confirmation"])
        self.assertFalse(result["second_confirmation_required"])


if __name__ == "__main__":
    unittest.main()
