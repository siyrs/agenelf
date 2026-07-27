from __future__ import annotations

import unittest
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class DockerOpsRuntimeWiringTest(unittest.TestCase):
    def test_compose_uses_lifecycle_aware_unified_runner_without_expanding_secret_mounts(self) -> None:
        compose = yaml.safe_load(
            (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        )
        runner = compose["services"]["ops-runner"]
        self.assertEqual(
            runner["command"],
            [
                "python",
                "/agenelf/scripts/runner_supervisor.py",
                "--name",
                "ops-runner",
                "--heartbeat-interval",
                "1",
                "--",
                "python",
                "/agenelf/scripts/ops_runner_entry.py",
                "--interval",
                "1",
            ],
        )
        volumes = runner["volumes"]
        self.assertTrue(any("local/secrets" in item and item.endswith(":ro") for item in volumes))
        self.assertFalse(any("local/memory" in item for item in volumes))
        self.assertFalse(any("local/self" in item for item in volumes))
        self.assertIn(
            "./data/runner-health:/agenelf/data/runner-health:rw",
            volumes,
        )

    def test_chat_entrypoint_delegates_single_resume_attempt_to_cli(self) -> None:
        script = (PROJECT_ROOT / "scripts" / "chat.sh").read_text(encoding="utf-8")
        self.assertIn("/agenelf/app-fork/cli.py", script)
        self.assertNotIn("/agenelf/app-fork/resume.py", script)
        self.assertIn('-e AGENELF_SKIP_AUTO_RESUME=', script)

    def test_direct_cli_invocation_also_attempts_resume(self) -> None:
        source = (PROJECT_ROOT / "app" / "cli.py").read_text(encoding="utf-8")
        resume_index = source.index("resume_pending_task(")
        agent_index = source.index("agent = Agent(config)")
        self.assertLess(resume_index, agent_index)
        self.assertIn("AGENELF_SKIP_AUTO_RESUME", source)

    def test_default_tool_round_budget_supports_multi_step_recovery(self) -> None:
        config = yaml.safe_load(
            (PROJECT_ROOT / "app" / "config.yaml").read_text(encoding="utf-8")
        )
        self.assertGreaterEqual(config["agent"]["max_tool_rounds"], 16)


if __name__ == "__main__":
    unittest.main(verbosity=2)
