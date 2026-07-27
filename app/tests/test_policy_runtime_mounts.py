from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]


class PolicyRuntimeMountTest(unittest.TestCase):
    def test_policy_driven_runtime_services_receive_read_only_policy(self):
        compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        services = compose["services"]
        for name in ("agenelf", "ops-runner", "repair-runner"):
            with self.subTest(service=name):
                volumes = services[name].get("volumes", [])
                self.assertIn("./policy:/agenelf/policy:ro", volumes)

    def test_node_validation_runner_enforces_alias_only_requests_without_policy_or_secrets_mounts(self):
        compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        service = compose["services"]["validation-runner"]
        volumes = [str(item) for item in service.get("volumes", [])]
        self.assertNotIn("./policy:/agenelf/policy:ro", volumes)
        self.assertFalse(any("local/secrets" in item for item in volumes))
        self.assertIn("./local/validation.yaml:/agenelf/local/validation.yaml:ro", volumes)
        source = (ROOT / "node" / "packages" / "core" / "src" / "validation.ts").read_text(encoding="utf-8")
        self.assertIn('capability: CAPABILITY', source)
        self.assertIn('request.risk !== "read"', source)
        self.assertIn("验证请求不得携带自由参数", source)
        self.assertIn("验证请求指纹不匹配", source)

    def test_execution_policy_modules_are_host_gate_protected(self):
        text = (ROOT / "scripts" / "gate_check.sh").read_text(encoding="utf-8")
        for path in (
            "core/policy.py",
            "core/execution_policy.py",
            "core/capabilities.py",
        ):
            self.assertIn(path, text)

    def test_entrypoints_and_model_modules_are_host_gate_protected(self):
        text = (ROOT / "scripts" / "gate_check.sh").read_text(encoding="utf-8")
        for path in (
            "api.py",
            "cli.py",
            "core/agent.py",
            "core/llm.py",
            "core/interactive_prompt.py",
            "core/context.py",
            "skills/docker_ops.py",
            "skills/authorized_upgrade_recovery.py",
            "skills/self_optimize.py",
        ):
            self.assertIn(path, text)


if __name__ == "__main__":
    unittest.main()
