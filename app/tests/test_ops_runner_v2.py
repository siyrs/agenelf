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
SPEC = importlib.util.spec_from_file_location(
    "agenelf_ops_runner_v2", ROOT / "scripts" / "ops_runner_v2.py"
)
ops_runner_v2 = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = ops_runner_v2
SPEC.loader.exec_module(ops_runner_v2)


class FakeSession:
    commands: list[str] = []
    stdout = "ok\n"
    stderr = ""
    exit_code = 0

    def __init__(self, profile, secrets_root):
        self.profile = profile
        self.secrets_root = secrets_root

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def run(self, command: str, timeout: int = 120):
        self.__class__.commands.append(command)
        return ops_runner_v2.legacy.CommandResult(
            command,
            self.__class__.exit_code,
            self.__class__.stdout,
            self.__class__.stderr,
        )

    def write_text(self, remote_path: str, content: str):
        raise AssertionError("Docker diagnostics must not write remote files")


class EnhancedOpsRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_root = os.environ.get("AGENELF_ROOT")
        os.environ["AGENELF_ROOT"] = str(self.root)
        self.servers_file = self.root / "servers.yaml"
        self._write_profiles("primary")
        FakeSession.commands = []
        FakeSession.stdout = "ok\n"
        FakeSession.stderr = ""
        FakeSession.exit_code = 0
        self.runner = ops_runner_v2.EnhancedOpsRunner(
            root=self.root,
            servers_file=self.servers_file,
            secrets_root=self.root / "secrets",
            session_factory=FakeSession,
        )

    def tearDown(self):
        if self.old_root is None:
            os.environ.pop("AGENELF_ROOT", None)
        else:
            os.environ["AGENELF_ROOT"] = self.old_root
        self.tmp.cleanup()

    def _write_profiles(self, *aliases: str) -> None:
        rows = ["servers:"]
        for alias in aliases:
            rows.extend(
                [
                    f"  {alias}:",
                    "    host: 127.0.0.1",
                    "    username: agenelf",
                    "    docker_command: docker",
                    "    allowed_operations: [inspect, docker_ps, service_restart]",
                ]
            )
        self.servers_file.write_text("\n".join(rows) + "\n", encoding="utf-8")

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

    def test_new_server_alias_is_visible_without_runner_restart(self):
        self._write_profiles("primary", "secondary")
        request = operations.submit_operation(
            "server.operations",
            "inspect",
            "secondary",
            {},
            operations.RISK_READ,
            "inspect hot-added alias",
            root=self.root,
        )
        counts = self.runner.run_once()
        self.assertEqual(counts.get("succeeded"), 1)
        state = operations.get_operation(request["id"], root=self.root)
        self.assertEqual(state["status"], "succeeded")
        self.assertIn("secondary", self.runner.profiles)

    def test_docker_logs_execute_read_only_without_owner_decision(self):
        request = operations.submit_operation(
            "server.docker",
            "docker_logs",
            "primary",
            {"container": "sing-box", "tail": 120},
            operations.RISK_READ,
            "logs",
            root=self.root,
        )
        self.assertEqual(self.runner.run_once().get("succeeded"), 1)
        self.assertIn(
            "docker logs --tail 120 --timestamps sing-box",
            FakeSession.commands[-1],
        )
        state = operations.get_operation(request["id"], root=self.root)
        self.assertEqual(state["status"], "succeeded")

    def test_docker_diagnose_does_not_render_environment_values(self):
        operations.submit_operation(
            "server.docker",
            "docker_diagnose",
            "primary",
            {"container": "sing-box", "tail": 50},
            operations.RISK_READ,
            "diagnose",
            root=self.root,
        )
        self.assertEqual(self.runner.run_once().get("succeeded"), 1)
        command = FakeSession.commands[-1]
        self.assertIn("EnvCount={{len .Config.Env}}", command)
        self.assertNotIn("json .Config.Env", command)
        self.assertNotIn("docker exec", command)

    def test_docker_restart_waits_for_fingerprint_bound_approval(self):
        request = operations.submit_operation(
            "server.docker",
            "docker_restart",
            "primary",
            {"container": "sing-box"},
            operations.RISK_CHANGE,
            "restart",
            root=self.root,
        )
        self.assertEqual(self.runner.run_once().get("pending"), 1)
        self.assertEqual(FakeSession.commands, [])
        self._approve(request)
        self.assertEqual(self.runner.run_once().get("succeeded"), 1)
        self.assertIn("docker restart --time 10 sing-box", FakeSession.commands[-1])
        self.assertIn("docker inspect --type container", FakeSession.commands[-1])

    def test_invalid_container_is_rejected_before_ssh(self):
        request = operations.submit_operation(
            "server.docker",
            "docker_logs",
            "primary",
            {"container": "sing-box; id", "tail": 50},
            operations.RISK_READ,
            "bad",
            root=self.root,
        )
        self.assertEqual(self.runner.run_once().get("failed"), 1)
        state = operations.get_operation(request["id"], root=self.root)
        self.assertEqual(state["status"], "failed")
        self.assertIn("非法 Docker 容器名称", state["result"]["reason"])
        self.assertEqual(FakeSession.commands, [])

    def test_runner_redacts_common_secrets_from_result_evidence(self):
        FakeSession.stdout = "token=super-secret-value sk-1234567890abcdef\n"
        request = operations.submit_operation(
            "server.docker",
            "docker_logs",
            "primary",
            {"container": "sing-box", "tail": 20},
            operations.RISK_READ,
            "logs",
            root=self.root,
        )
        self.assertEqual(self.runner.run_once().get("succeeded"), 1)
        state = operations.get_operation(request["id"], root=self.root)
        stdout = state["result"]["commands"][0]["stdout"]
        self.assertNotIn("super-secret-value", stdout)
        self.assertNotIn("1234567890abcdef", stdout)
        self.assertIn("[REDACTED]", stdout)

    def test_invalid_reload_keeps_last_known_good_profiles(self):
        self.servers_file.write_text("servers: [broken", encoding="utf-8")
        request = operations.submit_operation(
            "server.operations",
            "inspect",
            "primary",
            {},
            operations.RISK_READ,
            "inspect with transient bad config",
            root=self.root,
        )
        self.assertEqual(self.runner.run_once().get("succeeded"), 1)
        state = operations.get_operation(request["id"], root=self.root)
        self.assertEqual(state["status"], "succeeded")
        audit = (self.root / "logs" / "ops-runner.log").read_text(encoding="utf-8")
        self.assertIn("profiles_reload_failed", audit)


if __name__ == "__main__":
    unittest.main(verbosity=2)
