from __future__ import annotations

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


class RuntimeSourceAndCliPaletteTest(unittest.TestCase):
    def test_all_python_runtimes_mount_current_app_source(self):
        compose = yaml.safe_load(
            (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        )
        services = compose["services"]
        for name in (
            "agenelf",
            "ops-runner",
            "approval-runner",
            "validation-runner",
            "repair-runner",
        ):
            volumes = [str(item) for item in services[name]["volumes"]]
            self.assertIn(
                "./app:/agenelf/app-fork:ro",
                volumes,
                msg=f"{name} must run current app source",
            )
            self.assertNotIn(
                "./app-fork:/agenelf/app-fork:ro",
                volumes,
                msg=f"{name} must not run a stale app-fork copy",
            )
            self.assertEqual(
                services[name]["environment"]["AGENELF_RUNTIME_SOURCE"],
                "app-bind",
            )

    def test_cli_uses_palette_for_input_hint_and_dispatch_aliases(self):
        source = (ROOT / "app" / "cli.py").read_text(encoding="utf-8")
        self.assertIn("InteractivePrompt", source)
        self.assertIn("prompt.read()", source)
        self.assertIn("command_hint()", source)
        self.assertIn("canonical_command", source)
        self.assertIn('command == "/approvals"', source)
        self.assertNotIn(
            "命令：/self /assess /scorecard",
            source,
            msg="startup command text must come from the shared catalogue",
        )

    def test_prompt_toolkit_is_a_declared_runtime_dependency(self):
        requirements = (ROOT / "app" / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("prompt-toolkit", requirements)

    def test_completion_is_enabled_by_default(self):
        config = yaml.safe_load(
            (ROOT / "app" / "config.yaml").read_text(encoding="utf-8")
        )
        self.assertTrue(config["cli"]["interactive_completion"])
        self.assertGreaterEqual(config["cli"]["command_menu_rows"], 8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
