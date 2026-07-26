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
        self.assertEqual(broker["network_mode"], "none")
        self.assertFalse(any("local/secrets" in item for item in broker_volumes))
        self.assertEqual(
            broker["command"],
            ["python", "/agenelf/scripts/approval_runner.py", "--interval", "0.25"],
        )

    def test_key_init_is_isolated_and_shared_read_only(self):
        compose = yaml.safe_load(
            (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        )
        services = compose["services"]
        init = services["approval-key-init"]
        self.assertEqual(init["network_mode"], "none")
        self.assertEqual(init["user"], "0:0")
        self.assertIn(
            "approval-key:/agenelf/approval:ro",
            services["agenelf"]["volumes"],
        )
        self.assertIn(
            "approval-key:/agenelf/approval:ro",
            services["approval-runner"]["volumes"],
        )
        self.assertIn("approval-key", compose["volumes"])

    def test_windows_and_shell_wrappers_use_same_python_implementation(self):
        powershell = (ROOT / "scripts/approve.ps1").read_text(encoding="utf-8")
        shell = (ROOT / "scripts/approve.sh").read_text(encoding="utf-8")
        self.assertIn("approve.py", powershell)
        self.assertIn("approve.py", shell)
        self.assertNotIn("auth-decisions", shell)

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
