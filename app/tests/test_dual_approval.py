"""双签（多票）审批与策略版本绑定的回归测试。

覆盖：
* 策略引擎给出 owner_elevated / owner_irreversible 时，请求升级为双签并缩短 TTL；
* check_auth 仅在宿主机裁决文件中不同 decided_by 票数达标时返回 approved；
* approve.sh 实测：双票路径、同人重复投票（退出码 3）、deny 一票即决、单签兼容；
* 一次性核销（consume）在双签下仍成立。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from core import permissions

FAKE_POLICY_VERSION = "9.9.9-test"


def _install_fake_engine(evaluation: dict) -> None:
    """注入 duck-type 假策略引擎（模拟并行实现的 core.policy 契约）。"""

    module = types.ModuleType("core.policy")

    class PolicyEngine:
        policy_version = evaluation.get("policy_version", FAKE_POLICY_VERSION)

        def evaluate(self, capability, operation, subject="agent"):
            return dict(evaluation)

        def approval_requirements(self, approval_mode):
            return []

    module.PolicyEngine = PolicyEngine  # type: ignore[attr-defined]
    sys.modules["core.policy"] = module


def _remove_fake_engine() -> None:
    sys.modules.pop("core.policy", None)


class DualApprovalPolicyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_root = os.environ.get("AGENELF_ROOT")
        os.environ["AGENELF_ROOT"] = str(self.root)

    def tearDown(self):
        _remove_fake_engine()
        if self.old_root is None:
            os.environ.pop("AGENELF_ROOT", None)
        else:
            os.environ["AGENELF_ROOT"] = self.old_root
        self.tmp.cleanup()

    def _request(self, rid: str) -> dict:
        return json.loads(
            (self.root / "data" / "auth-requests" / f"{rid}.json").read_text(
                encoding="utf-8"
            )
        )

    def _write_multi_decision(
        self,
        rid: str,
        approvers: list[str],
        decision: str = "approve",
    ) -> None:
        """模拟宿主机 approve.sh 产生的多票裁决文件。"""

        request = self._request(rid)
        now = datetime.now().astimezone()
        path = self.root / "data" / "auth-decisions" / f"{rid}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "request_id": rid,
                    "decision": decision,
                    "required_approvers": 2,
                    "approvals": [
                        {
                            "decided_by": name,
                            "decided_at": now.isoformat(timespec="seconds"),
                        }
                        for name in approvers
                    ],
                    "fingerprint": request["fingerprint"],
                    "decided_at": now.isoformat(timespec="seconds"),
                    "decided_by": approvers[-1] if approvers else "unknown",
                    "expires_at": (now + timedelta(minutes=3)).isoformat(
                        timespec="seconds"
                    ),
                }
            ),
            encoding="utf-8",
        )

    def test_elevated_request_requires_dual_signature_and_short_ttl(self):
        _install_fake_engine(
            {
                "allowed": True,
                "risk": "privileged",
                "approval": "owner_elevated",
                "auto_execute": False,
                "requires_textual_confirmation": True,
                "second_confirmation_required": False,
                "rollback_required": True,
                "reason": "privileged 操作",
                "policy_version": FAKE_POLICY_VERSION,
            }
        )
        ok, rid = permissions.request_auth(
            "server_ops",
            "docker_install",
            "primary",
            operation="docker_install",
        )
        self.assertTrue(ok)
        request = self._request(rid)
        self.assertEqual(request["required_approvers"], 2)
        self.assertTrue(request["require_distinct_humans"])
        self.assertEqual(request["approval_mode"], "owner_elevated")
        self.assertEqual(request["policy_version"], FAKE_POLICY_VERSION)
        self.assertEqual(request["approvals"], [])
        created = datetime.fromisoformat(request["created_at"])
        expires = datetime.fromisoformat(request["expires_at"])
        self.assertEqual(
            (expires - created).total_seconds(), permissions.ELEVATED_TTL_SECONDS
        )
        audit_log = (self.root / "logs" / "audit.log").read_text(encoding="utf-8")
        self.assertIn(f"policy_version={FAKE_POLICY_VERSION}", audit_log)

    def test_irreversible_request_requires_second_confirmation(self):
        _install_fake_engine(
            {
                "allowed": True,
                "risk": "irreversible",
                "approval": "owner_irreversible",
                "auto_execute": False,
                "requires_textual_confirmation": True,
                "second_confirmation_required": True,
                "rollback_required": True,
                "reason": "不可逆操作",
                "policy_version": FAKE_POLICY_VERSION,
            }
        )
        ok, rid = permissions.request_auth(
            "server_ops", "database_drop", "primary", operation="database_drop"
        )
        self.assertTrue(ok)
        request = self._request(rid)
        self.assertEqual(request["required_approvers"], 2)
        self.assertTrue(request["second_confirmation_required"])
        created = datetime.fromisoformat(request["created_at"])
        expires = datetime.fromisoformat(request["expires_at"])
        self.assertEqual(
            (expires - created).total_seconds(), permissions.IRREVERSIBLE_TTL_SECONDS
        )

    def test_two_distinct_approvers_approve(self):
        _install_fake_engine(
            {
                "allowed": True,
                "risk": "privileged",
                "approval": "owner_elevated",
                "auto_execute": False,
                "policy_version": FAKE_POLICY_VERSION,
            }
        )
        binding = {"target": "primary", "operation": "docker_install", "parameters": {}}
        ok, rid = permissions.request_auth(
            "server_ops", "docker_install", "primary", binding=binding,
            operation="docker_install",
        )
        self.assertTrue(ok)
        self.assertEqual(permissions.check_auth(rid), permissions.STATUS_PENDING)

        # 收集中（一票）：仍 pending
        self._write_multi_decision(rid, ["alice"], decision="collecting")
        self.assertEqual(permissions.check_auth(rid), permissions.STATUS_PENDING)

        # 同一批准人两票只算一票：仍 pending
        self._write_multi_decision(rid, ["alice", "alice"])
        self.assertEqual(permissions.check_auth(rid), permissions.STATUS_PENDING)

        # 两名不同批准人：approved
        self._write_multi_decision(rid, ["alice", "bob"])
        self.assertEqual(
            permissions.check_auth(rid, expected_binding=binding),
            permissions.STATUS_APPROVED,
        )

        # 一次性核销在双签下仍成立
        self.assertTrue(permissions.consume_auth(rid, expected_binding=binding))
        self.assertFalse(permissions.consume_auth(rid, expected_binding=binding))
        self.assertEqual(permissions.check_auth(rid), permissions.STATUS_USED)

    def test_dual_request_deny_is_final(self):
        _install_fake_engine(
            {
                "allowed": True,
                "risk": "privileged",
                "approval": "owner_elevated",
                "auto_execute": False,
                "policy_version": FAKE_POLICY_VERSION,
            }
        )
        ok, rid = permissions.request_auth(
            "server_ops", "docker_install", "primary", operation="docker_install"
        )
        self.assertTrue(ok)
        self._write_multi_decision(rid, ["carol"], decision="deny")
        self.assertEqual(permissions.check_auth(rid), permissions.STATUS_DENIED)

    def test_engine_unavailable_keeps_single_signature_behavior(self):
        sys.modules["core.policy"] = None  # 模拟 import 失败路径
        ok, rid = permissions.request_auth("x", "y", "z")
        self.assertTrue(ok)
        request = self._request(rid)
        self.assertNotIn("required_approvers", request)
        self.assertNotIn("policy_version", request)
        created = datetime.fromisoformat(request["created_at"])
        expires = datetime.fromisoformat(request["expires_at"])
        self.assertEqual(
            (expires - created).total_seconds(), permissions.DEFAULT_TTL_SECONDS
        )


class ApproveScriptDualTest(unittest.TestCase):
    """临时布局下用真实 bash 驱动 approve.sh 的多票路径。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "scripts").mkdir()
        source = Path(__file__).resolve().parents[2] / "scripts" / "approve.sh"
        shutil.copy(source, self.root / "scripts" / "approve.sh")
        self.old_root = os.environ.get("AGENELF_ROOT")
        os.environ["AGENELF_ROOT"] = str(self.root)

    def tearDown(self):
        if self.old_root is None:
            os.environ.pop("AGENELF_ROOT", None)
        else:
            os.environ["AGENELF_ROOT"] = self.old_root
        self.tmp.cleanup()

    def _run(self, *args):
        return subprocess.run(
            ["bash", "scripts/approve.sh", *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def _craft_request(self, required: int | None = None, ttl: int = 180) -> str:
        rid = f"auth-{uuid.uuid4().hex[:12]}"
        binding = {
            "skill": "server_ops",
            "action": "docker_install",
            "detail": "primary",
        }
        now = datetime.now().astimezone()
        data = {
            "schema_version": 2,
            "id": rid,
            "skill": binding["skill"],
            "action": binding["action"],
            "detail": binding["detail"],
            "reason": "",
            "channel": "cli",
            "binding": binding,
            "fingerprint": permissions.binding_fingerprint(binding),
            "created_at": now.isoformat(timespec="seconds"),
            "expires_at": (now + timedelta(seconds=ttl)).isoformat(timespec="seconds"),
            "ttl_seconds": ttl,
            "approvals": [],
        }
        if required and required > 1:
            data["required_approvers"] = required
            data["require_distinct_humans"] = True
            data["approval_mode"] = "owner_elevated"
            data["policy_version"] = FAKE_POLICY_VERSION
        directory = self.root / "data" / "auth-requests"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{rid}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return rid

    def _decision(self, rid: str) -> dict:
        return json.loads(
            (self.root / "data" / "auth-decisions" / f"{rid}.json").read_text(
                encoding="utf-8"
            )
        )

    def test_dual_vote_reaches_quorum_and_consumes_once(self):
        rid = self._craft_request(required=2)

        first = self._run(rid, "approve", "--as", "alice")
        self.assertEqual(first.returncode, 0, msg=first.stdout + first.stderr)
        decision = self._decision(rid)
        self.assertEqual(decision["decision"], "collecting")
        self.assertEqual(len(decision["approvals"]), 1)
        self.assertEqual(permissions.check_auth(rid), permissions.STATUS_PENDING)

        second = self._run(rid, "approve", "--as", "bob")
        self.assertEqual(second.returncode, 0, msg=second.stdout + second.stderr)
        decision = self._decision(rid)
        self.assertEqual(decision["decision"], "approve")
        self.assertEqual(len(decision["approvals"]), 2)
        self.assertEqual(permissions.check_auth(rid), permissions.STATUS_APPROVED)

        # 一次性核销仍成立
        self.assertTrue(permissions.consume_auth(rid))
        self.assertFalse(permissions.consume_auth(rid))
        self.assertEqual(permissions.check_auth(rid), permissions.STATUS_USED)

    def test_same_approver_second_vote_rejected_with_code_3(self):
        rid = self._craft_request(required=2)
        first = self._run(rid, "approve", "--as", "alice")
        self.assertEqual(first.returncode, 0, msg=first.stdout + first.stderr)
        duplicate = self._run(rid, "approve", "--as", "alice")
        self.assertEqual(duplicate.returncode, 3)
        self.assertIn("重复投票", duplicate.stderr)
        self.assertEqual(permissions.check_auth(rid), permissions.STATUS_PENDING)

    def test_deny_is_final_even_mid_collection(self):
        rid = self._craft_request(required=2)
        first = self._run(rid, "approve", "--as", "alice")
        self.assertEqual(first.returncode, 0, msg=first.stdout + first.stderr)
        denied = self._run(rid, "deny", "--as", "carol")
        self.assertEqual(denied.returncode, 0, msg=denied.stdout + denied.stderr)
        self.assertEqual(permissions.check_auth(rid), permissions.STATUS_DENIED)
        late = self._run(rid, "approve", "--as", "bob")
        self.assertNotEqual(late.returncode, 0)
        self.assertIn("不允许覆盖", late.stderr)

    def test_single_signature_regression(self):
        rid = self._craft_request(required=None)
        result = self._run(rid, "approve")
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        decision = self._decision(rid)
        self.assertEqual(decision["decision"], "approve")
        self.assertEqual(decision["schema_version"], 1)
        self.assertNotIn("approvals", decision)
        self.assertEqual(permissions.check_auth(rid), permissions.STATUS_APPROVED)
        again = self._run(rid, "approve")
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("不允许覆盖", again.stderr)

    def test_default_user_is_voter_without_as_flag(self):
        rid = self._craft_request(required=2)
        first = self._run(rid, "approve")
        self.assertEqual(first.returncode, 0, msg=first.stdout + first.stderr)
        decision = self._decision(rid)
        voter = decision["approvals"][0]["decided_by"]
        self.assertTrue(voter)
        duplicate = self._run(rid, "approve")
        self.assertEqual(duplicate.returncode, 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
