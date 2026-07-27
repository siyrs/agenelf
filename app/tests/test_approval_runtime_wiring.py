from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


class ApprovalRuntimeWiringTest(unittest.TestCase):
    def test_compose_keeps_final_decisions_read_only_for_agent(self):
        compose = yaml.safe_load(
            (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        )
        services = compose["services"]
        agent = services["agenelf"]
        broker = services["approval-runner"]
        agent_volumes = [str(item) for item in agent["volumes"]]
        broker_volumes = [str(item) for item in broker["volumes"]]
        self.assertIn(
            "./data/auth-decisions:/agenelf/data/auth-decisions:ro",
            agent_volumes,
        )
        self.assertIn(
            "./data/auth-decisions:/agenelf/data/auth-decisions:rw",
            broker_volumes,
        )
        self.assertIn(
            "./data/approval-commands:/agenelf/data/approval-commands:ro",
            broker_volumes,
        )
        self.assertIn(
            "./data/approval-results:/agenelf/data/approval-results:rw",
            broker_volumes,
        )
        self.assertIn(
            "./data/runner-health:/agenelf/data/runner-health:rw",
            broker_volumes,
        )
        self.assertIn(
            "./data/runner-health:/agenelf/data/runner-health:ro",
            agent_volumes,
        )
        self.assertEqual(broker["network_mode"], "none")
        self.assertFalse(any("local/secrets" in item for item in broker_volumes))
        self.assertEqual(
            broker["command"],
            [
                "python",
                "/agenelf/scripts/runner_supervisor.py",
                "--name",
                "approval-runner",
                "--heartbeat-interval",
                "0.5",
                "--",
                "python",
                "/agenelf/scripts/approval_runner.py",
                "--interval",
                "0.25",
            ],
        )

    def test_key_init_is_isolated_and_shared_read_only(self):
        compose = yaml.safe_load(
            (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        )
        services = compose["services"]
        init = services["approval-key-init"]
        self.assertEqual(init["network_mode"], "none")
        self.assertEqual(init["user"], "0:0")
        # 模型常驻进程绝不可见 HMAC 密钥，防止自签审批。
        self.assertFalse(
            any(
                "approval-key" in str(item)
                for item in services["agenelf"]["volumes"]
            ),
            "agenelf 服务不得挂载 approval-key",
        )
        # 只有独立 cli 服务（profile=cli）与 approval-runner 可读密钥。
        cli = services["cli"]
        self.assertIn("cli", cli["profiles"])
        self.assertIn(
            "approval-key:/agenelf/approval:ro",
            cli["volumes"],
        )
        self.assertIn(
            "approval-key:/agenelf/approval:ro",
            services["approval-runner"]["volumes"],
        )
        self.assertIn("approval-key", compose["volumes"])

    def test_agent_gets_web_console_assets_read_only(self):
        compose = yaml.safe_load(
            (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        )
        volumes = compose["services"]["agenelf"]["volumes"]
        self.assertIn("./web:/agenelf/web:ro", volumes)

    def test_agent_reads_approval_commands_but_cannot_write_them(self):
        compose = yaml.safe_load(
            (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        )
        volumes = compose["services"]["agenelf"]["volumes"]
        self.assertIn(
            "./data/approval-commands:/agenelf/data/approval-commands:ro",
            volumes,
        )

    def test_chat_sh_uses_isolated_cli_service(self):
        script = (ROOT / "scripts" / "chat.sh").read_text(encoding="utf-8")
        self.assertIn("--profile cli", script)
        self.assertIn("run --rm", script)
        self.assertIn("/agenelf/app-fork/cli.py", script)

    def test_windows_and_shell_wrappers_prefer_same_python_implementation(self):
        powershell = (ROOT / "scripts/approve.ps1").read_text(encoding="utf-8")
        shell = (ROOT / "scripts/approve.sh").read_text(encoding="utf-8")
        self.assertIn("approve.py", powershell)
        self.assertIn("approve.py", shell)
        # Normal repository execution delegates before entering the deliberately
        # retained standalone fallback used by old copied-script installations.
        self.assertLess(shell.index("approve.py"), shell.index("DECISIONS_DIR"))
        self.assertIn('if [[ -f "${SCRIPT_DIR}/approve.py" ]]', shell)

    def test_host_approval_prefers_app_source_over_stale_runtime_copy(self):
        script = (ROOT / "scripts/approve.py").read_text(encoding="utf-8")
        self.assertIn('SOURCE_APP = ROOT / "app"', script)
        self.assertIn('RUNTIME_APP = ROOT / "app-fork"', script)
        self.assertIn(
            "APP_DIR = SOURCE_APP if SOURCE_APP.is_dir() else RUNTIME_APP",
            script,
        )
        self.assertLess(script.index("SOURCE_APP"), script.index("RUNTIME_APP"))

    def test_native_windows_sync_mirrors_app_and_handles_robocopy_exit_codes(self):
        script = (ROOT / "scripts/sync_fork.ps1").read_text(encoding="utf-8")
        self.assertIn('Join-Path $Root "app"', script)
        self.assertIn('Join-Path $Root "app-fork"', script)
        self.assertIn('"/MIR"', script)
        self.assertIn('"/XD", "__pycache__"', script)
        self.assertIn('"/XF", "*.pyc"', script)
        self.assertIn("if ($Code -ge 8)", script)

    def test_approval_key_init_is_persistent(self):
        script = ROOT / "scripts/init_approval_key.py"
        spec = importlib.util.spec_from_file_location(
            "init_approval_key_under_test", script
        )
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key"
            first = module.initialize(path)
            content = path.read_bytes()
            second = module.initialize(path)
            self.assertTrue(first["created"])
            self.assertFalse(second["created"])
            self.assertEqual(path.read_bytes(), content)
            self.assertGreaterEqual(len(content.strip()), 32)

    def test_cli_does_not_write_auth_decisions_directly(self):
        cli = (ROOT / "app" / "cli.py").read_text(encoding="utf-8")
        self.assertIn("handle_owner_decision", cli)
        self.assertNotIn("auth-decisions", cli)
        self.assertLess(
            cli.index("handle_owner_decision("),
            cli.index("agent.chat(user_input"),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
