"""validation 执行面的策略咨询回归测试。

* 引擎可用：read 级 run_check/run_suite 正常提交并记录 policy_version；
  被拒绝或策略风险更严格时失败关闭（验证 Runner 无审批通道）。
* 引擎缺失（import 失败）：完全降级为既有行为，别名/操作约束不变。
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

from core import validation

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


class PolicyEnforcementValidationTest(unittest.TestCase):
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
        return self.root / "data" / "validation-requests"

    def test_read_check_submits_with_policy_version(self):
        _install_fake_engine(
            {
                "allowed": True,
                "risk": "read",
                "approval": "none",
                "auto_execute": True,
                "policy_version": FAKE_POLICY_VERSION,
            }
        )
        request = validation.submit_validation(
            "run_check", "unit-fast", "运行验证检查 unit-fast", root=self.root
        )
        self.assertEqual(request["risk"], "read")
        self.assertEqual(request["policy_version"], FAKE_POLICY_VERSION)
        self.assertEqual(request["approval_mode"], "none")
        self.assertTrue((self._requests_dir() / f"{request['id']}.json").is_file())
        self.assertEqual(
            validation.get_validation(request["id"], root=self.root)["status"],
            "queued",
        )

    def test_denied_suite_never_creates_request(self):
        _install_fake_engine(
            {
                "allowed": False,
                "risk": "forbidden",
                "approval": "impossible",
                "auto_execute": False,
                "reason": "策略禁止该验证",
                "policy_version": FAKE_POLICY_VERSION,
            }
        )
        with self.assertRaises(PermissionError):
            validation.submit_validation(
                "run_suite", "nightly", "运行验证套件 nightly", root=self.root
            )
        self.assertFalse(self._requests_dir().exists())

    def test_stricter_policy_risk_fails_closed(self):
        _install_fake_engine(
            {
                "allowed": True,
                "risk": "change",
                "approval": "owner_exact",
                "auto_execute": False,
                "policy_version": FAKE_POLICY_VERSION,
            }
        )
        with self.assertRaises(PermissionError):
            validation.submit_validation(
                "run_check", "unit-fast", "运行验证检查 unit-fast", root=self.root
            )
        self.assertFalse(self._requests_dir().exists())

    def test_missing_engine_degrades_to_existing_behavior(self):
        sys.modules["core.policy"] = None  # 模拟 import 失败路径
        request = validation.submit_validation(
            "run_suite", "nightly", "运行验证套件 nightly", root=self.root
        )
        self.assertNotIn("policy_version", request)
        self.assertEqual(
            validation.get_validation(request["id"], root=self.root)["status"],
            "queued",
        )
        # 既有操作/别名约束保持不变
        with self.assertRaises(ValueError):
            validation.submit_validation("drop_tables", "x", "非法操作", root=self.root)
        with self.assertRaises(ValueError):
            validation.submit_validation("run_check", "", "空目标", root=self.root)


if __name__ == "__main__":
    unittest.main(verbosity=2)
