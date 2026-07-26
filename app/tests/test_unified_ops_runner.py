from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from core import operations

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "agenelf_unified_ops_runner", SCRIPTS / "unified_ops_runner.py"
)
unified = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = unified
SPEC.loader.exec_module(unified)


class FakeSession:
    commands: list[str] = []
    fail_contains: str | None = None

    def __init__(self, profile, secrets_root):
        self.profile = profile
        self.secrets_root = secrets_root

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def run(self, command: str, timeout: int = 120):
        self.__class__.commands.append(command)
        failed = bool(self.__class__.fail_contains and self.__class__.fail_contains in command)
        if failed:
            return unified.CommandResult(command, 1, "", "boom\n")
        if " logs " in command:
            output = (
                "startup failed vless://uuid@example.com:443?token=top-secret\n"
                "password=hunter2\n"
            )
        elif " inspect " in command:
            output = '{"Name":"/sing-box","Labels":{"subscription":"https://x/s?token=abc"}}\n'
        else:
            output = "ok\n"
        return unified.CommandResult(command, 0, output, "")


class UnifiedOpsRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_root = os.environ.get("AGENELF_ROOT")
        os.environ["AGENELF_ROOT"] = str(self.root)
        self.servers = self.root / "servers.yaml"
        self._write_servers(["primary"])
        FakeSession.commands = []
        FakeSession.fail_contains = None
        self.runner = unified.UnifiedOpsRunner(
            root=self.root,
            servers_file=self.servers,
            secrets_root=self.root / "secrets",
            session_factory=FakeSession,
        )

    def tearDown(self) -> None:
        if self.old_root is None:
            os.environ.pop("AGENELF_ROOT", None)
        else:
            os.environ["AGENELF_ROOT"] = self.old_root
        self.tmp.cleanup()

    def _write_servers(self, names: list[str]) -> None:
        blocks = []
        for name in names:
            blocks.append(
                f"""  {name}:
    host: 127.0.0.1
    username: agenelf
    docker_command: docker
    allowed_docker_operations: [get_docker_logs, inspect_docker_container, run_docker_check, restart_docker_container]
    allowed_containers: [sing-box]
    docker_checks:
      sing-box-config:
        container: sing-box
        argv: [sing-box, check, -c, /etc/sing-box/config.json]
"""
            )
        self.servers.write_text("servers:\n" + "".join(blocks), encoding="utf-8")

    def _submit(self, operation: str, target: str = "primary", parameters=None):
        risk = unified._DOCKER_RISKS[operation]
        return operations.submit_operation(
            "docker.operations",
            operation,
            target,
            parameters or {},
            risk,
            operation,
            root=self.root,
        )

    def _approve(self, request: dict) -> None:
        path = self.root / "data" / "auth-decisions" / f"{request['id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "request_id": request["id"],
                    "decision": "approve",
                    "fingerprint": request["fingerprint"],
                    "expires_at": (
                        datetime.now().astimezone() + timedelta(minutes=5)
                    ).isoformat(timespec="seconds"),
                }
            ),
            encoding="utf-8",
        )

    def test_server_profiles_hot_reload_without_runner_restart(self) -> None:
        self.assertNotIn("pve-ubuntu", self.runner.profiles)
        self._write_servers(["primary", "pve-ubuntu"])
        request = self._submit(
            "get_docker_logs",
            target="pve-ubuntu",
            parameters={"container": "sing-box", "tail": 50},
        )
        counts = self.runner.run_once()
        self.assertEqual(counts.get("succeeded"), 1)
        self.assertIn("pve-ubuntu", self.runner.profiles)
        state = operations.get_operation(request["id"], root=self.root)
        self.assertEqual(state["status"], "succeeded")

    def test_malformed_profile_reload_keeps_last_known_good_snapshot(self) -> None:
        self.servers.write_text("servers: [broken", encoding="utf-8")
        request = self._submit(
            "get_docker_logs",
            parameters={"container": "sing-box", "tail": 10},
        )
        self.assertEqual(self.runner.run_once().get("succeeded"), 1)
        self.assertEqual(operations.get_operation(request["id"], root=self.root)["status"], "succeeded")
        audit = (self.root / "logs" / "ops-runner.log").read_text(encoding="utf-8")
        self.assertIn("profiles_reload_failed", audit)

    def test_logs_and_inspect_outputs_are_redacted(self) -> None:
        logs = self._submit(
            "get_docker_logs",
            parameters={"container": "sing-box", "tail": 100},
        )
        inspect = self._submit(
            "inspect_docker_container", parameters={"container": "sing-box"}
        )
        counts = self.runner.run_once()
        self.assertEqual(counts.get("succeeded"), 2)
        log_state = operations.get_operation(logs["id"], root=self.root)
        log_dump = json.dumps(log_state, ensure_ascii=False)
        self.assertIn("vless://[REDACTED]", log_dump)
        self.assertNotIn("top-secret", log_dump)
        self.assertNotIn("hunter2", log_dump)
        inspect_state = operations.get_operation(inspect["id"], root=self.root)
        inspect_dump = json.dumps(inspect_state, ensure_ascii=False)
        self.assertNotIn("token=abc", inspect_dump)
        inspect_command = inspect_state["result"]["commands"][0]["command"]
        self.assertNotIn("Config.Env", inspect_command)

    def test_restart_waits_for_exact_approval_then_reports_status(self) -> None:
        request = self._submit(
            "restart_docker_container",
            parameters={"container": "sing-box", "timeout_seconds": 12},
        )
        self.assertEqual(self.runner.run_once().get("pending"), 1)
        self.assertFalse(FakeSession.commands)
        self._approve(request)
        self.assertEqual(self.runner.run_once().get("succeeded"), 1)
        joined = "\n".join(FakeSession.commands)
        self.assertIn("docker restart --time 12 sing-box", joined)
        self.assertIn("docker ps -a", joined)

    def test_preconfigured_check_never_accepts_model_command_arguments(self) -> None:
        request = self._submit(
            "run_docker_check", parameters={"check": "sing-box-config"}
        )
        self.assertEqual(self.runner.run_once().get("succeeded"), 1)
        self.assertIn(
            "docker exec sing-box sing-box check -c /etc/sing-box/config.json",
            FakeSession.commands[-1],
        )
        self.assertEqual(request["parameters"], {"check": "sing-box-config"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
