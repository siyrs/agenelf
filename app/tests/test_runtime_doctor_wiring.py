from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from core.execution_policy import resolve_contract
from core.interactive_prompt import command_names
from skills import runtime_doctor

ROOT = Path(__file__).resolve().parents[2]


class RuntimeDoctorWiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.compose = yaml.safe_load(
            (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        )

    def test_all_long_running_runners_use_the_supervisor_and_writable_heartbeat_mount(self) -> None:
        expected_children = {
            "ops-runner": "/agenelf/scripts/unified_ops_runner.py",
            "approval-runner": "/agenelf/scripts/approval_runner.py",
            "self-upgrade-runner": "/agenelf/scripts/self_upgrade_runner_entry.py",
            "validation-runner": "/agenelf/scripts/validation_runner.py",
            "repair-runner": "/agenelf/scripts/repair_runner.py",
        }
        services = self.compose["services"]
        for name, child in expected_children.items():
            with self.subTest(runner=name):
                service = services[name]
                command = service["command"]
                self.assertEqual(command[0:2], ["python", "/agenelf/scripts/runner_supervisor.py"])
                self.assertIn("--name", command)
                self.assertEqual(command[command.index("--name") + 1], name)
                self.assertIn("--", command)
                self.assertIn(child, command)
                self.assertIn(
                    "./data/runner-health:/agenelf/data/runner-health:rw",
                    [str(item) for item in service["volumes"]],
                )

    def test_agent_observes_heartbeats_read_only(self) -> None:
        volumes = [str(item) for item in self.compose["services"]["agenelf"]["volumes"]]
        self.assertIn(
            "./data/runner-health:/agenelf/data/runner-health:ro",
            volumes,
        )
        self.assertNotIn(
            "./data/runner-health:/agenelf/data/runner-health:rw",
            volumes,
        )

    def test_runtime_doctor_is_a_read_only_pure_tool(self) -> None:
        contract = resolve_contract("runtime_doctor", {}, runtime_doctor)
        self.assertIsNotNone(contract)
        self.assertEqual(contract.capability, "agent.runtime_doctor")
        self.assertEqual(contract.risk, "read")
        self.assertEqual(contract.execution_mode, "pure")

    def test_cli_palette_dispatch_and_initialization_are_wired(self) -> None:
        self.assertIn("/doctor", command_names())
        cli = (ROOT / "app" / "cli.py").read_text(encoding="utf-8")
        self.assertIn('command == "/doctor"', cli)
        self.assertIn('runtime_doctor', cli)
        initializer = (ROOT / "scripts" / "init_local.py").read_text(encoding="utf-8")
        self.assertIn('ROOT / "data" / "runner-health"', initializer)

    def test_supervisor_source_does_not_log_child_argv_or_environment(self) -> None:
        source = (ROOT / "scripts" / "runner_supervisor.py").read_text(encoding="utf-8")
        self.assertIn("shell=False", source)
        self.assertNotIn('value["command"]', source)
        self.assertNotIn('value["argv"]', source)
        self.assertNotIn('value["environment"]', source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
