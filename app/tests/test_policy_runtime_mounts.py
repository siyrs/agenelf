from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]


class PolicyRuntimeMountTest(unittest.TestCase):
    def test_every_runtime_service_receives_read_only_policy(self):
        compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        services = compose["services"]
        for name in ("agenelf", "ops-runner", "validation-runner", "repair-runner"):
            with self.subTest(service=name):
                volumes = services[name].get("volumes", [])
                self.assertIn("./policy:/agenelf/policy:ro", volumes)

    def test_execution_policy_modules_are_host_gate_protected(self):
        text = (ROOT / "scripts" / "gate_check.sh").read_text(encoding="utf-8")
        for path in (
            "core/policy.py",
            "core/execution_policy.py",
            "core/capabilities.py",
        ):
            self.assertIn(path, text)


if __name__ == "__main__":
    unittest.main()
