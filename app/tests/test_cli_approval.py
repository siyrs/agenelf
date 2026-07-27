"""Direct tests for core.cli_approval's deterministic owner-decision plane.

Complements tests/test_cli_owner_approval.py (happy-path broker flow) by covering
the branch structure with a fake agent, a recording rich Console and monkeypatched
``owner_approval``/``approval_catalog`` collaborators:

* unparseable input is returned to ordinary chat (``False``);
* ambiguous and failing request resolution;
* the degraded fallback guidance when the approval channel (HMAC control key)
  is unavailable;
* failed broker results, deny short-circuit and auto-continue gating.
"""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from rich.console import Console

from core import cli_approval, owner_approval


class FakeAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def chat(self, message, subject="agent"):
        self.calls.append((message, subject))
        return "已继续并验证"


def _selected(request_id: str = "op-0123456789abcdef", kind: str = "operation"):
    return {
        "id": request_id,
        "kind": kind,
        "operation": "compose_deploy",
        "target": "pve-ubuntu",
        "summary": "map 10808:1080",
    }


def _broker_ok(decision: str = "approve"):
    return {
        "status": "succeeded",
        "decision": {"decision": decision, "superseded_duplicates": []},
    }


class CliApprovalBranchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.console = Console(record=True, width=120)
        self.agent = FakeAgent()
        # Keep broker wait budget deterministic regardless of the host env.
        env_patch = patch.dict(os.environ, {}, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for name in (
            "AGENELF_APPROVAL_WAIT_SECONDS",
            "AGENELF_APPROVAL_AUTO_CONTINUE",
        ):
            os.environ.pop(name, None)

    def output(self) -> str:
        # export_text() clears the record buffer by default; keep it so each
        # test can assert repeatedly on the same snapshot.
        return self.console.export_text(clear=False)

    def _resolve(self, selected):
        return patch.object(
            cli_approval.approval_catalog,
            "resolve_pending_request",
            return_value=(selected, []),
        )

    def _broker(self, result):
        return (
            patch.object(
                cli_approval.owner_approval,
                "submit_owner_command",
                return_value={"id": "owner-decision-0123456789abcdef"},
            ),
            patch.object(
                cli_approval.owner_approval,
                "wait_for_command_result",
                return_value=result,
            ),
        )

    # -- input parsing ------------------------------------------------------

    def test_ordinary_text_is_returned_to_chat(self) -> None:
        handled = cli_approval.handle_owner_decision(
            agent=self.agent,
            raw_input="帮我分析一下这个升级方案的风险",
            console=self.console,
            config={},
        )
        self.assertFalse(handled)
        self.assertEqual(self.agent.calls, [])
        self.assertEqual(self.output(), "")

    def test_empty_input_is_not_a_decision(self) -> None:
        handled = cli_approval.handle_owner_decision(
            agent=self.agent,
            raw_input="   ",
            console=self.console,
            config={},
        )
        self.assertFalse(handled)

    # -- resolution failure branches ---------------------------------------

    def test_ambiguous_decision_asks_for_exact_id_and_lists_pending(self) -> None:
        pending = [
            _selected("op-aaaaaaaaaaaaaaaa"),
            _selected("op-bbbbbbbbbbbbbbbb"),
        ]
        submit, _wait = self._broker(_broker_ok())
        with (
            patch.object(
                cli_approval.approval_catalog,
                "resolve_pending_request",
                side_effect=owner_approval.AmbiguousApprovalError(
                    "文本审批命中多个待审批请求", pending=pending
                ),
            ),
            submit as submit_mock,
        ):
            handled = cli_approval.handle_owner_decision(
                agent=self.agent,
                raw_input="审批通过",
                console=self.console,
                config={},
            )
        self.assertTrue(handled)
        text = self.output()
        self.assertIn("需要明确请求 ID", text)
        self.assertIn("op-aaaaaaaaaaaaaaaa", text)
        self.assertIn("op-bbbbbbbbbbbbbbbb", text)
        # Ambiguity must never reach the signing broker or the model.
        submit_mock.assert_not_called()
        self.assertEqual(self.agent.calls, [])

    def test_resolution_approval_error_is_reported_without_broker(self) -> None:
        submit, _wait = self._broker(_broker_ok())
        with (
            patch.object(
                cli_approval.approval_catalog,
                "resolve_pending_request",
                side_effect=owner_approval.ApprovalError("没有等待审批的请求"),
            ),
            submit as submit_mock,
        ):
            handled = cli_approval.handle_owner_decision(
                agent=self.agent,
                raw_input="/approve op-0123456789abcdef",
                console=self.console,
                config={},
            )
        self.assertTrue(handled)
        self.assertIn("没有等待审批的请求", self.output())
        submit_mock.assert_not_called()
        self.assertEqual(self.agent.calls, [])

    # -- degraded approval channel -----------------------------------------

    def test_missing_control_key_degrades_to_manual_fallback_guidance(self) -> None:
        with (
            self._resolve(_selected()),
            patch.object(
                cli_approval.owner_approval,
                "submit_owner_command",
                side_effect=owner_approval.ApprovalError(
                    "审批控制密钥不可用：/agenelf/approval/key"
                ),
            ),
        ):
            handled = cli_approval.handle_owner_decision(
                agent=self.agent,
                raw_input="/approve op-0123456789abcdef",
                console=self.console,
                config={},
            )
        self.assertTrue(handled)
        text = self.output()
        self.assertIn("审批通道不可用", text)
        self.assertIn("审批控制密钥不可用", text)
        # The owner gets an out-of-band deterministic path instead of a crash.
        self.assertIn("approve.ps1 op-0123456789abcdef approve", text)
        self.assertIn("approve.py op-0123456789abcdef approve", text)
        self.assertIn("approval-runner ops-runner", text)
        self.assertEqual(self.agent.calls, [])

    def test_fallback_guidance_uses_deny_action_and_upgrade_services(self) -> None:
        selected = _selected("auth-0123456789ab", kind="authorization")
        with (
            self._resolve(selected),
            patch.object(
                cli_approval.owner_approval,
                "submit_owner_command",
                side_effect=OSError("broker socket unavailable"),
            ),
        ):
            handled = cli_approval.handle_owner_decision(
                agent=self.agent,
                raw_input="/deny auth-0123456789ab 先不升级",
                console=self.console,
                config={},
            )
        self.assertTrue(handled)
        text = self.output()
        self.assertIn("approve.ps1 auth-0123456789ab deny", text)
        self.assertIn("approval-runner self-upgrade-runner", text)

    def test_control_key_bytes_requires_readable_key_file(self) -> None:
        with patch.dict(
            os.environ,
            {"AGENELF_APPROVAL_KEY_FILE": "/nonexistent/agenelf-test-key"},
        ):
            with self.assertRaises(owner_approval.ApprovalError) as ctx:
                owner_approval._control_key_bytes()
        self.assertIn("审批控制密钥不可用", str(ctx.exception))

    def test_unsuccessful_broker_result_shows_error_and_fallback(self) -> None:
        submit, wait = self._broker(
            {"status": "failed", "error": "审批代理没有在限定时间内响应"}
        )
        with self._resolve(_selected()), submit, wait:
            handled = cli_approval.handle_owner_decision(
                agent=self.agent,
                raw_input="/approve op-0123456789abcdef",
                console=self.console,
                config={},
            )
        self.assertTrue(handled)
        text = self.output()
        self.assertIn("审批失败", text)
        self.assertIn("审批代理没有在限定时间内响应", text)
        self.assertIn("approve.ps1", text)
        self.assertEqual(self.agent.calls, [])

    # -- decision panel / continuation gating -------------------------------

    def test_superseded_duplicates_are_reported(self) -> None:
        result = _broker_ok()
        result["decision"]["superseded_duplicates"] = ["op-aaaaaaaaaaaaaaaa"]
        submit, wait = self._broker(result)
        with (
            self._resolve(_selected()),
            submit,
            wait,
            patch.object(
                cli_approval.operations,
                "wait_for_result",
                return_value={"status": "approved"},
            ),
        ):
            handled = cli_approval.handle_owner_decision(
                agent=self.agent,
                raw_input="/approve op-0123456789abcdef",
                console=self.console,
                config={"cli": {"approval_auto_continue": False}},
            )
        self.assertTrue(handled)
        self.assertIn("已自动拒绝同载荷重复请求", self.output())
        self.assertIn("op-aaaaaaaaaaaaaaaa", self.output())

    def test_operation_execution_result_is_printed(self) -> None:
        submit, wait = self._broker(_broker_ok())
        with (
            self._resolve(_selected()),
            submit,
            wait,
            patch.object(
                cli_approval.operations,
                "wait_for_result",
                return_value={"result": {"exit_code": 0, "output": "done"}},
            ),
        ):
            handled = cli_approval.handle_owner_decision(
                agent=self.agent,
                raw_input="/approve op-0123456789abcdef",
                console=self.console,
                config={"cli": {"approval_auto_continue": False}},
            )
        self.assertTrue(handled)
        self.assertIn("运维执行结果", self.output())
        self.assertIn("done", self.output())

    def test_operation_result_type_error_is_tolerated(self) -> None:
        submit, wait = self._broker(_broker_ok())
        with (
            self._resolve(_selected()),
            submit,
            wait,
            patch.object(
                cli_approval.operations,
                "wait_for_result",
                side_effect=TypeError("unexpected signature"),
            ),
        ):
            handled = cli_approval.handle_owner_decision(
                agent=self.agent,
                raw_input="/approve op-0123456789abcdef",
                console=self.console,
                config={"cli": {"approval_auto_continue": False}},
            )
        self.assertTrue(handled)

    def test_auto_continue_disabled_by_env_skips_model(self) -> None:
        submit, wait = self._broker(_broker_ok())
        with (
            patch.dict(os.environ, {"AGENELF_APPROVAL_AUTO_CONTINUE": "0"}),
            self._resolve(_selected()),
            submit,
            wait,
            patch.object(
                cli_approval.operations,
                "wait_for_result",
                return_value={"status": "approved"},
            ),
        ):
            handled = cli_approval.handle_owner_decision(
                agent=self.agent,
                raw_input="/approve op-0123456789abcdef",
                console=self.console,
                config={"cli": {"approval_auto_continue": True}},
            )
        self.assertTrue(handled)
        self.assertEqual(self.agent.calls, [])

    def test_deny_never_continues_with_model(self) -> None:
        submit, wait = self._broker(_broker_ok(decision="deny"))
        with self._resolve(_selected()), submit, wait:
            handled = cli_approval.handle_owner_decision(
                agent=self.agent,
                raw_input="/deny op-0123456789abcdef 暂不修改",
                console=self.console,
                config={"cli": {"approval_auto_continue": True}},
            )
        self.assertTrue(handled)
        self.assertIn("deny", self.output())
        self.assertEqual(self.agent.calls, [])

    def test_wait_seconds_env_is_bounded_for_broker(self) -> None:
        submit, wait = self._broker(_broker_ok())
        with (
            patch.dict(os.environ, {"AGENELF_APPROVAL_WAIT_SECONDS": "3600"}),
            self._resolve(_selected()),
            submit as submit_mock,
            wait as wait_mock,
            patch.object(
                cli_approval.operations,
                "wait_for_result",
                return_value={"status": "approved"},
            ),
        ):
            handled = cli_approval.handle_owner_decision(
                agent=self.agent,
                raw_input="/approve op-0123456789abcdef",
                console=self.console,
                config={"cli": {"approval_auto_continue": False}},
            )
        self.assertTrue(handled)
        # wait_for_command_result timeout is capped at 30s regardless of env.
        self.assertLessEqual(wait_mock.call_args.kwargs["timeout_seconds"], 30.0)
        # command TTL grows with the configured wait window but stays >= 15s.
        self.assertGreaterEqual(submit_mock.call_args.kwargs["ttl_seconds"], 15)


class CliApprovalPendingViewTest(unittest.TestCase):
    def setUp(self) -> None:
        self.console = Console(record=True, width=120)

    def test_show_pending_empty(self) -> None:
        with patch.object(
            cli_approval.approval_catalog,
            "list_pending_requests",
            return_value=[],
        ):
            cli_approval.show_pending(self.console)
        self.assertIn("当前没有等待主人审批的请求", self.console.export_text())

    def test_show_pending_lists_kinds_and_hint(self) -> None:
        rows = [
            {
                "id": "op-0123456789abcdef",
                "kind": "operation",
                "operation": "compose_deploy",
                "target": "pve-ubuntu",
                "summary": "map ports",
            },
            {
                "id": "auth-0123456789ab",
                "kind": "authorization",
                "operation": "authorize_upgrade_intent",
                "target": "upgrade",
                "summary": "runner",
            },
        ]
        with patch.object(
            cli_approval.approval_catalog,
            "list_pending_requests",
            return_value=rows,
        ):
            cli_approval.show_pending(self.console)
        text = self.console.export_text()
        self.assertIn("op-0123456789abcdef", text)
        self.assertIn("auth-0123456789ab", text)
        self.assertIn("运维", text)
        self.assertIn("授权", text)
        self.assertIn("/approve", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
