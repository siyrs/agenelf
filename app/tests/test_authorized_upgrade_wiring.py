from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from core.execution_policy import resolve_contract
from skills import authorized_self_upgrade

ROOT = Path(__file__).resolve().parents[2]


class AuthorizedUpgradeWiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.compose = yaml.safe_load(
            (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        )

    def test_self_upgrade_runner_is_networkless_and_has_no_secrets_or_docker_socket(self) -> None:
        runner = self.compose["services"]["self-upgrade-runner"]
        self.assertEqual(
            runner["command"],
            ["python", "/agenelf/scripts/self_upgrade_runner.py", "--interval", "1"],
        )
        self.assertEqual(runner["network_mode"], "none")
        self.assertTrue(runner["read_only"])
        self.assertEqual(runner["cap_drop"], ["ALL"])
        volumes = [str(item) for item in runner["volumes"]]
        joined = "\n".join(volumes)
        self.assertNotIn("local/secrets", joined)
        self.assertNotIn("local/profile", joined)
        self.assertNotIn("local/memory", joined)
        self.assertNotIn("docker.sock", joined)
        self.assertNotIn("/.git", joined)
        self.assertNotIn("/.env:/", joined)
        self.assertIn("./app-tmp:/agenelf/app-tmp:ro", volumes)
        self.assertIn(
            "./data/auth-decisions:/agenelf/data/auth-decisions:ro",
            volumes,
        )
        self.assertIn(
            "./data/auth-consumed:/agenelf/data/auth-consumed:rw",
            volumes,
        )
        self.assertIn("./app:/agenelf/upgrade-target/app:rw", volumes)
        self.assertIn("./scripts:/agenelf/upgrade-target/scripts:rw", volumes)
        self.assertIn("./policy:/agenelf/upgrade-target/policy:rw", volumes)

    def test_agent_cannot_write_repository_source_directly(self) -> None:
        agent = self.compose["services"]["agenelf"]
        volumes = [str(item) for item in agent["volumes"]]
        self.assertIn("./app:/agenelf/app-fork:ro", volumes)
        self.assertIn("./app-tmp:/agenelf/app-tmp:rw", volumes)
        self.assertIn("./.github:/agenelf/repo-source/.github:ro", volumes)
        self.assertFalse(
            any(
                item.startswith("./app:/agenelf/upgrade-target")
                or item.startswith("./scripts:/agenelf/upgrade-target")
                for item in volumes
            )
        )

    def test_all_upgrade_tools_have_explicit_execution_contracts(self) -> None:
        expected = {
            "request_authorized_self_upgrade": ("change", "local_state"),
            "continue_authorized_self_upgrade": ("change", "controlled_sandbox"),
            "authorized_self_upgrade_status": ("read", "pure"),
            "list_authorized_upgrade_scopes": ("read", "pure"),
        }
        for tool, pair in expected.items():
            with self.subTest(tool=tool):
                contract = resolve_contract(tool, {}, authorized_self_upgrade)
                self.assertIsNotNone(contract)
                self.assertEqual(contract.capability, "agent.authorized_self_upgrade")
                self.assertEqual((contract.risk, contract.execution_mode), pair)

    def test_cli_palette_and_dispatch_include_upgrade_and_auth_ids(self) -> None:
        prompt = (ROOT / "app" / "core" / "interactive_prompt.py").read_text(
            encoding="utf-8"
        )
        cli = (ROOT / "app" / "cli.py").read_text(encoding="utf-8")
        self.assertIn('SlashCommand("/upgrade"', prompt)
        self.assertIn('command == "/upgrade"', cli)
        self.assertIn("cmd_upgrade(agent, rest)", cli)
        self.assertIn("approval_catalog.list_pending_requests", prompt)
        self.assertIn("authorized_self_upgrade_status", cli)

    def test_initialization_creates_all_upgrade_evidence_directories(self) -> None:
        source = (ROOT / "scripts" / "init_local.py").read_text(encoding="utf-8")
        for name in (
            "authorized-upgrades",
            "self-upgrade-requests",
            "self-upgrade-results",
            "self-upgrade-locks",
            "self-upgrade-backups",
        ):
            self.assertIn(name, source)

    def test_policy_defines_two_stage_owner_authorization_and_permanent_redlines(self) -> None:
        policy = yaml.safe_load(
            (ROOT / "policy" / "safety-constraints.v1.yaml").read_text(
                encoding="utf-8"
            )
        )
        upgrade = policy["owner_authorized_upgrade"]
        self.assertTrue(upgrade["enabled"])
        self.assertEqual(
            [item["name"] for item in upgrade["stages"]],
            ["intent_scope_approval", "tested_candidate_approval"],
        )
        self.assertTrue(upgrade["execution"]["backup_before_apply"])
        self.assertTrue(upgrade["execution"]["rollback_on_partial_failure"])
        redlines = set(upgrade["permanent_redlines"])
        self.assertIn("no_self_approval_or_forged_owner_decision", redlines)
        self.assertIn("no_access_to_env_local_secrets_ssh_keys_or_approval_key", redlines)
        self.assertIn("no_direct_push_or_merge_main_from_autonomous_runtime", redlines)


if __name__ == "__main__":
    unittest.main(verbosity=2)
