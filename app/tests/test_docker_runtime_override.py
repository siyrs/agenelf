from __future__ import annotations

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


class DockerRuntimeOverrideTest(unittest.TestCase):
    def test_default_override_uses_current_app_for_every_python_runtime(self):
        override = yaml.safe_load(
            (ROOT / "docker-compose.override.yml").read_text(encoding="utf-8")
        )
        services = override["services"]
        for service_name in (
            "agenelf",
            "ops-runner",
            "validation-runner",
            "repair-runner",
        ):
            with self.subTest(service=service_name):
                volumes = services[service_name]["volumes"]
                self.assertIn("./app:/agenelf/app-fork:ro", volumes)

    def test_default_override_selects_hot_reloading_ops_runner(self):
        override = yaml.safe_load(
            (ROOT / "docker-compose.override.yml").read_text(encoding="utf-8")
        )
        command = override["services"]["ops-runner"]["command"]
        self.assertEqual(command[1], "/agenelf/scripts/ops_runner_v2.py")
        self.assertEqual(command[-2:], ["--interval", "1"])

    def test_base_compose_still_preserves_read_only_and_least_privilege(self):
        base = yaml.safe_load(
            (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        )
        for service_name in (
            "agenelf",
            "ops-runner",
            "validation-runner",
            "repair-runner",
        ):
            with self.subTest(service=service_name):
                service = base["services"][service_name]
                self.assertTrue(service["read_only"])
                self.assertEqual(service["cap_drop"], ["ALL"])
                self.assertIn("no-new-privileges:true", service["security_opt"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
