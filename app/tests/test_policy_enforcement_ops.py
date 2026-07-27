"""operations 执行面的策略咨询回归测试。

* 引擎可用：read 类操作保持自动执行直通；被拒绝/红线操作在提交期失败关闭；
  策略与既有判定冲突时取更严格者。
* 引擎缺失（import 失败）：完全降级为既有行为。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

from core import operations

FAKE_POLICY_VERSION = "9.9.9-test"


def _install_fake_engine(evaluation: dict) -> None:
    module = types.ModuleType("core.policy")

    class PolicyEngine:
        policy_version = evaluation.get("policy_version", FAKE_POLICY_VERSION)

        def evaluate(self, capability, operation, subject="agent"):
            return dict(evaluation)

        def approval_requirements(self, approval_mode):
            return []

    module.PolicyEngine = PolicyEngine  # type: ignore[attr-defined]
    sys.modules["core.policy"] = module


class PolicyEnforcementOpsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_root = os.environ.get("AGENELF_ROOT")
        os.environ["AGENELF_ROOT"] = str(self.root)

    def tearDown(self):
        sys.modules.pop("core.policy", None)
        if self.old_root is None:
            os.environ.pop("AGENELF_ROOT", None)
        else:
            os.environ["AGENELF_ROOT"] = self.old_root
        self.tmp.cleanup()

    def _requests_dir(self) -> Path:
        return self.root / "data" / "ops-requests"

    def test_read_operation_auto_execute_passthrough(self):
        _install_fake_engine(
            {
                "allowed": True,
                "risk": "read",
                "approval": "none",
                "auto_execute": True,
                "policy_version": FAKE_POLICY_VERSION,
            }
        )
        request = operations.submit_operation(
            "server.operations", "inspect", "primary", {}, operations.RISK_READ, "巡检"
        )
        self.assertEqual(request["risk"], operations.RISK_READ)
        self.assertEqual(request["policy_version"], FAKE_POLICY_VERSION)
        self.assertEqual(request["approval_mode"], "none")
        self.assertEqual(
            operations.get_operation(request["id"])["status"], "queued"
        )

    def test_denied_operation_never_creates_request(self):
        _install_fake_engine(
            {
                "allowed": False,
                "risk": "forbidden",
                "approval": "impossible",
                "auto_execute": False,
                "reason": "raw_shell 属于治理绕过",
                "policy_version": FAKE_POLICY_VERSION,
            }
        )
        with self.assertRaises(PermissionError):
            operations.submit_operation(
                "server.operations",
                "raw_shell",
                "primary",
                {"command": "id"},
                operations.RISK_CHANGE,
                "任意 shell",
            )
        self.assertFalse(self._requests_dir().exists())

    def test_impossible_approval_blocks_even_when_allowed_flag_true(self):
        _install_fake_engine(
            {
                "allowed": True,
                "risk": "read",
                "approval": "impossible",
                "auto_execute": True,
                "reason": "契约异常样例",
                "policy_version": FAKE_POLICY_VERSION,
            }
        )
        with self.assertRaises(PermissionError):
            operations.submit_operation(
                "server.operations", "inspect", "primary", {}, operations.RISK_READ, "巡检"
            )
        self.assertFalse(self._requests_dir().exists())

    def test_stricter_policy_risk_wins_over_declared(self):
        _install_fake_engine(
            {
                "allowed": True,
                "risk": "privileged",
                "approval": "owner_elevated",
                "auto_execute": False,
                "policy_version": FAKE_POLICY_VERSION,
            }
        )
        request = operations.submit_operation(
            "server.operations", "inspect", "primary", {}, operations.RISK_READ, "巡检"
        )
        self.assertEqual(request["risk"], operations.RISK_PRIVILEGED)
        self.assertEqual(request["declared_risk"], operations.RISK_READ)
        self.assertEqual(
            operations.get_operation(request["id"])["status"], "awaiting_approval"
        )

    def test_auto_execute_false_escalates_read_to_change(self):
        _install_fake_engine(
            {
                "allowed": True,
                "risk": "read",
                "approval": "owner_exact",
                "auto_execute": False,
                "policy_version": FAKE_POLICY_VERSION,
            }
        )
        request = operations.submit_operation(
            "server.operations", "inspect", "primary", {}, operations.RISK_READ, "巡检"
        )
        self.assertEqual(request["risk"], operations.RISK_CHANGE)
        self.assertEqual(
            operations.get_operation(request["id"])["status"], "awaiting_approval"
        )

    def test_irreversible_policy_risk_fails_closed(self):
        _install_fake_engine(
            {
                "allowed": True,
                "risk": "irreversible",
                "approval": "owner_irreversible",
                "auto_execute": False,
                "policy_version": FAKE_POLICY_VERSION,
            }
        )
        with self.assertRaises(PermissionError):
            operations.submit_operation(
                "server.operations",
                "database_drop",
                "primary",
                {},
                operations.RISK_CHANGE,
                "删库",
            )
        self.assertFalse(self._requests_dir().exists())

    def test_missing_engine_degrades_to_existing_behavior(self):
        sys.modules["core.policy"] = None  # 模拟 import 失败路径
        read_request = operations.submit_operation(
            "server.operations", "inspect", "primary", {}, operations.RISK_READ, "巡检"
        )
        self.assertNotIn("policy_version", read_request)
        self.assertEqual(
            operations.get_operation(read_request["id"])["status"], "queued"
        )
        change_request = operations.submit_operation(
            "server.operations",
            "apt_update",
            "primary",
            {},
            operations.RISK_CHANGE,
            "更新 APT",
        )
        self.assertNotIn("policy_version", change_request)
        self.assertEqual(
            operations.get_operation(change_request["id"])["status"],
            "awaiting_approval",
        )
        # 既有红线仍生效
        with self.assertRaises(PermissionError):
            operations.submit_operation(
                "server.operations",
                "raw_shell",
                "primary",
                {},
                operations.RISK_FORBIDDEN,
                "禁止",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
