from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from core import authorized_upgrade


ROOT = Path(__file__).resolve().parents[2]


class NodeSecretOpsRunnerTest(unittest.TestCase):
    def test_overlay_isolates_secret_runner_and_owner_console(self) -> None:
        overlay = yaml.safe_load(
            (ROOT / "compose.override.yaml").read_text(encoding="utf-8")
        )
        services = overlay["services"]
        staging_init = services["secret-staging-init"]
        runner = services["secret-ops-runner"]
        console = services["secret-cli"]

        self.assertIn("secret-staging", overlay["volumes"])
        self.assertEqual(staging_init["user"], "0:0")
        self.assertEqual(staging_init["network_mode"], "none")
        self.assertTrue(staging_init["read_only"])
        self.assertEqual(staging_init["restart"], "no")
        self.assertEqual(staging_init["cap_drop"], ["ALL"])
        self.assertEqual(set(staging_init["cap_add"]), {"CHOWN", "FOWNER"})

        self.assertEqual(runner["build"]["dockerfile"], "Dockerfile.ops-secret")
        self.assertIn("node/apps/secret-ops-runner/src/main.ts", runner["command"])
        self.assertTrue(runner["read_only"])
        self.assertEqual(console["profiles"], ["secret-cli"])
        self.assertIn("node/apps/secret-cli/src/main.ts", console["entrypoint"])
        self.assertEqual(console["command"], ["help"])
        self.assertTrue(console["stdin_open"])
        self.assertTrue(console["tty"])

        init_volumes = "\n".join(str(item) for item in staging_init["volumes"])
        runner_volumes = "\n".join(str(item) for item in runner["volumes"])
        console_volumes = "\n".join(str(item) for item in console["volumes"])
        self.assertIn(
            "secret-staging:/agenelf/local/secret-staging:rw", init_volumes
        )
        for required in (
            "./local/servers.yaml:/agenelf/local/servers.yaml:ro",
            "./local/env-secrets.yaml:/agenelf/local/env-secrets.yaml:ro",
            "./local/secrets:/agenelf/local/secrets:ro",
            "secret-staging:/agenelf/local/secret-staging:rw",
            "./data/ops-requests:/agenelf/data/ops-requests:ro",
            "./data/auth-decisions:/agenelf/data/auth-decisions:ro",
            "./data/ops-results:/agenelf/data/ops-results:rw",
            "./data/ops-locks:/agenelf/data/ops-locks:rw",
            "./data/ops-events:/agenelf/data/ops-events:rw",
        ):
            self.assertIn(required, runner_volumes)
        self.assertIn(
            "./data/ops-requests:/agenelf/data/ops-requests:rw", console_volumes
        )
        self.assertIn(
            "secret-staging:/agenelf/local/secret-staging:rw", console_volumes
        )
        for forbidden in (
            "/agenelf/approval",
            "docker.sock",
            "local/memory",
            "local/self",
            "/agenelf/app-fork",
            "/agenelf/policy",
        ):
            self.assertNotIn(forbidden, init_volumes)
            self.assertNotIn(forbidden, runner_volumes)
            self.assertNotIn(forbidden, console_volumes)

    def test_plaintext_chat_broker_is_internal_and_agent_still_has_no_ssh_secrets(self) -> None:
        top = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
        includes = [item["path"] if isinstance(item, dict) else item for item in top["include"]]
        self.assertIn("docker-compose.yml", includes)
        self.assertIn("compose.secret-chat.yaml", includes)

        document = yaml.safe_load(
            (ROOT / "compose.secret-chat.yaml").read_text(encoding="utf-8")
        )
        broker = document["services"]["secret-chat-broker"]
        self.assertEqual(broker["build"]["dockerfile"], "Dockerfile.ops-secret")
        self.assertIn("node/apps/secret-chat-broker/src/main.ts", broker["command"])
        self.assertNotIn("ports", broker)
        self.assertEqual(broker["expose"], ["8097"])
        self.assertTrue(broker["read_only"])
        self.assertEqual(broker["cap_drop"], ["ALL"])
        self.assertIn("no-new-privileges:true", broker["security_opt"])
        self.assertIn(".env", broker["env_file"])
        self.assertIn(".ops-runner.env", broker["env_file"])

        mounts = "\n".join(str(item) for item in broker["volumes"])
        for required in (
            "./local/servers.yaml:/agenelf/local/servers.yaml:ro",
            "./local/env-secrets.yaml:/agenelf/local/env-secrets.yaml:ro",
            "./local/secrets:/agenelf/local/secrets:ro",
            "./logs:/agenelf/logs:rw",
        ):
            self.assertIn(required, mounts)
        for forbidden in (
            "docker.sock",
            "/agenelf/approval",
            "local/memory",
            "local/self",
            "/agenelf/app-fork",
            "/agenelf/policy",
            "data/auth-decisions",
        ):
            self.assertNotIn(forbidden, mounts)

    def test_agent_and_normal_cli_never_mount_secret_material(self) -> None:
        base = yaml.safe_load(
            (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        )
        overlay = yaml.safe_load(
            (ROOT / "compose.override.yaml").read_text(encoding="utf-8")
        )
        for service_name in ("agenelf", "cli"):
            values = []
            values.extend(base["services"][service_name].get("volumes", []))
            values.extend(overlay["services"].get(service_name, {}).get("volumes", []))
            mounts = "\n".join(str(item) for item in values)
            for forbidden in (
                "/agenelf/local/secrets",
                "/agenelf/local/secret-staging",
                "/agenelf/local/env-secrets.yaml",
            ):
                self.assertNotIn(forbidden, mounts)

    def test_secret_ops_files_are_in_owner_authorized_scope(self) -> None:
        plan = authorized_upgrade.make_plan(
            "升级 Node secret env Ops、主人明文聊天 Broker 和 Secret Console",
            scopes=[
                "node_runners",
                "node_runtime",
                "node_skills",
                "node_build",
                "compose",
            ],
        )
        allowed = plan["allowed_paths"]
        for path in (
            "node/apps/secret-ops-runner/",
            "node/apps/secret-cli/",
            "node/apps/secret-chat-broker/",
            "node/packages/core/src/secret-ops.ts",
            "node/packages/core/src/secret-env.ts",
            "node/packages/core/src/secret-targets.ts",
            "node/packages/core/src/chat-secret-env.ts",
            "node/packages/core/src/secret-chat-client.ts",
            "node/packages/core/src/agent.ts",
            "node/packages/core/src/types.ts",
            "node/packages/skills/src/builtin.ts",
            "Dockerfile.ops-secret",
            "compose.yaml",
            "compose.override.yaml",
            "compose.secret-chat.yaml",
        ):
            self.assertIn(path, allowed)
        self.assertEqual(
            authorized_upgrade.SECRET_OPS_UPGRADE_POLICY_VERSION,
            "owner-authorized-secret-ops-v2",
        )

    def test_owner_secret_files_remain_git_ignored(self) -> None:
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("local/env-secrets.yaml", ignored)
        self.assertIn("local/secret-staging/", ignored)
        example = (ROOT / "local" / "env-secrets.example.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn("targets: {}", example)
        self.assertNotIn("sk-", example)


if __name__ == "__main__":
    unittest.main(verbosity=2)
