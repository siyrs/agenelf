from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import approval_catalog, authorized_upgrade, owner_approval, permissions
from skills import evolution_scope_guard


class AuthorizedUpgradePolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_root = os.environ.get("AGENELF_ROOT")
        os.environ["AGENELF_ROOT"] = str(self.root)

    def tearDown(self) -> None:
        if self.old_root is None:
            os.environ.pop("AGENELF_ROOT", None)
        else:
            os.environ["AGENELF_ROOT"] = self.old_root
        self.tmp.cleanup()

    def test_protected_goal_becomes_exact_two_stage_plan(self) -> None:
        plan = authorized_upgrade.make_plan(
            "升级 ops-runner 和 docker compose 生命周期能力",
            max_files=7,
            max_changed_lines=900,
        )
        self.assertIn("runners", plan["scopes"])
        self.assertIn("compose", plan["scopes"])
        self.assertIn("tests", plan["scopes"])
        self.assertIn("scripts/", plan["allowed_paths"])
        self.assertIn("docker-compose.yml", plan["allowed_paths"])
        self.assertTrue(plan["requires_candidate_approval"])
        self.assertEqual(plan["max_files"], 7)
        self.assertEqual(plan["max_changed_lines"], 900)
        self.assertRegex(plan["fingerprint"], r"^[0-9a-f]{64}$")

    def test_permanent_redlines_cannot_be_authorized(self) -> None:
        allowed = ["app/core/", "scripts/", "local/"]
        for path in (
            ".env",
            ".ops-runner.env",
            "local/secrets/id_ed25519",
            "data/auth-decisions/auth-x.json",
            ".git/config",
        ):
            with self.subTest(path=path):
                with self.assertRaises(authorized_upgrade.AuthorizedUpgradeError):
                    authorized_upgrade.validate_repo_path(path, allowed)

        with self.assertRaises(authorized_upgrade.AuthorizedUpgradeError):
            authorized_upgrade.scan_redlines(
                "app/skills/x.py",
                "SOCKET = '/var/run/docker.sock'\n",
            )
        with self.assertRaises(authorized_upgrade.AuthorizedUpgradeError):
            authorized_upgrade.scan_redlines(
                "scripts/x.py",
                "# 自动批准并伪造授权\n",
            )

    def test_intent_request_is_persisted_and_visible_in_unified_approvals(self) -> None:
        session = authorized_upgrade.create_or_get_session(
            "升级核心运行时但保持审批红线",
            scopes=["app_runtime", "tests"],
            root=self.root,
        )
        self.assertEqual(session["status"], "awaiting_intent_approval")
        auth_id = str(session["intent_auth_id"])
        self.assertTrue((self.root / "data" / "auth-requests" / f"{auth_id}.json").is_file())

        pending = approval_catalog.list_pending_requests(self.root)
        row = next(item for item in pending if item["id"] == auth_id)
        self.assertEqual(row["kind"], "authorization")
        selected, duplicates = approval_catalog.resolve_pending_request(auth_id, self.root)
        self.assertEqual(selected["id"], auth_id)
        self.assertEqual(selected["kind"], "authorization")
        self.assertEqual(duplicates, [])

        decision = owner_approval.apply_owner_decision(
            auth_id,
            "approve",
            decided_by="owner-test",
            root=self.root,
        )
        self.assertEqual(decision["decision"], "approve")
        binding = {key: value for key, value in session["plan"].items() if key != "fingerprint"}
        self.assertEqual(
            permissions.check_auth(auth_id, expected_binding=binding),
            permissions.STATUS_APPROVED,
        )

    def test_same_plan_reuses_active_session_instead_of_spamming_requests(self) -> None:
        first = authorized_upgrade.create_or_get_session(
            "升级 Compose Runner",
            scopes=["compose", "runners", "tests"],
            root=self.root,
        )
        second = authorized_upgrade.create_or_get_session(
            "升级 Compose Runner",
            scopes=["compose", "runners", "tests"],
            root=self.root,
        )
        self.assertEqual(first["id"], second["id"])
        requests = list((self.root / "data" / "auth-requests").glob("auth-*.json"))
        self.assertEqual(len(requests), 1)


class EvolutionScopeRoutingTest(unittest.TestCase):
    class FakeAgent:
        def __init__(self) -> None:
            self._calls: list[tuple[str, bool]] = []

        def run_autonomy_cycle(self, goal: str = "", apply_changes: bool = False):
            self._calls.append((goal, apply_changes))
            return {"status": "ordinary", "goal": goal}

    def test_ordinary_goal_still_uses_normal_sandbox(self) -> None:
        agent = self.FakeAgent()
        evolution_scope_guard.configure_runtime(agent=agent)
        result = agent.run_autonomy_cycle("优化一个普通解析器", apply_changes=True)
        self.assertEqual(result["status"], "ordinary")
        self.assertEqual(agent._calls, [("优化一个普通解析器", True)])

    def test_protected_goal_routes_to_authorized_upgrade_not_blanket_block(self) -> None:
        agent = self.FakeAgent()
        evolution_scope_guard.configure_runtime(agent=agent)
        routed = {
            "id": "upgrade-20260726-120000-12345678",
            "status": "awaiting_intent_approval",
            "next_action": "/approve auth-123456789abc",
        }
        with patch(
            "skills.authorized_self_upgrade.route_goal",
            return_value=routed,
        ) as mocked:
            result = agent.run_autonomy_cycle(
                "升级 docker compose runner 并补齐回归测试",
                apply_changes=True,
            )
        self.assertEqual(result["status"], "awaiting_intent_approval")
        self.assertNotEqual(result["status"], "host_review_required")
        self.assertIn("compose", result["matched_protected_scopes"])
        self.assertIn("runners", result["matched_protected_scopes"])
        mocked.assert_called_once()
        self.assertEqual(agent._calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
