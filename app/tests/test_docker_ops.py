from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from core import operations
from core.execution_policy import resolve_contract
from skills import docker_ops


class DockerOpsSkillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_root = os.environ.get("AGENELF_ROOT")
        self.old_servers = os.environ.get("AGENELF_SERVERS_FILE")
        os.environ["AGENELF_ROOT"] = str(self.root)
        self.servers = self.root / "servers.yaml"
        os.environ["AGENELF_SERVERS_FILE"] = str(self.servers)
        self._write_profile(["docker_ps", "service_restart"])

    def tearDown(self):
        if self.old_root is None:
            os.environ.pop("AGENELF_ROOT", None)
        else:
            os.environ["AGENELF_ROOT"] = self.old_root
        if self.old_servers is None:
            os.environ.pop("AGENELF_SERVERS_FILE", None)
        else:
            os.environ["AGENELF_SERVERS_FILE"] = self.old_servers
        self.tmp.cleanup()

    def _write_profile(self, operations_: list[str]) -> None:
        allowed = ", ".join(operations_)
        self.servers.write_text(
            f"""servers:
  primary:
    host: 127.0.0.1
    username: agenelf
    docker_command: docker
    allowed_operations: [{allowed}]
""",
            encoding="utf-8",
        )

    def _requests(self) -> list[dict]:
        paths = operations.queue_paths(self.root)
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(paths["requests"].glob("op-*.json"))
        ]

    def test_logs_uses_legacy_docker_read_grant_and_queues_exact_request(self):
        result = json.loads(
            docker_ops.docker_logs("primary", "sing-box", tail=80, wait_seconds=0)
        )
        self.assertEqual(result["status"], "queued")
        requests = self._requests()
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request["capability"], "server.docker")
        self.assertEqual(request["operation"], "docker_logs")
        self.assertEqual(
            request["parameters"], {"container": "sing-box", "tail": 80}
        )
        self.assertEqual(request["risk"], operations.RISK_READ)

    def test_diagnose_rejects_shell_metacharacters_before_queueing(self):
        result = docker_ops.docker_diagnose(
            "primary", "sing-box; id", tail=100, wait_seconds=0
        )
        self.assertIn("请求失败", result)
        self.assertIn("container", result)
        self.assertEqual(self._requests(), [])

    def test_restart_uses_legacy_read_plus_restart_grants_but_requires_approval(self):
        result = docker_ops.docker_restart("primary", "sing-box")
        self.assertIn("Docker 运维请求已创建", result)
        self.assertIn("批准命令", result)
        request = self._requests()[0]
        self.assertEqual(request["operation"], "docker_restart")
        self.assertEqual(request["risk"], operations.RISK_CHANGE)
        state = operations.get_operation(request["id"], root=self.root)
        self.assertEqual(state["status"], "awaiting_approval")

    def test_restart_is_denied_when_legacy_restart_grant_is_missing(self):
        self._write_profile(["docker_ps"])
        result = docker_ops.docker_restart("primary", "sing-box")
        self.assertIn("服务器策略未允许操作：docker_restart", result)
        self.assertEqual(self._requests(), [])

    def test_tail_is_strictly_bounded(self):
        result = docker_ops.docker_logs(
            "primary", "sing-box", tail=2001, wait_seconds=0
        )
        self.assertIn("tail 必须在 1 到 2000 之间", result)
        self.assertEqual(self._requests(), [])

    def test_all_docker_tools_have_queued_runner_contracts(self):
        expected = {
            "docker_logs": ("read", "queued_runner"),
            "docker_diagnose": ("read", "queued_runner"),
            "docker_restart": ("change", "queued_runner"),
        }
        for tool_name, (risk, mode) in expected.items():
            with self.subTest(tool=tool_name):
                contract = resolve_contract(tool_name, {}, docker_ops)
                self.assertIsNotNone(contract)
                self.assertEqual(contract.capability, "server.docker")
                self.assertEqual(contract.risk, risk)
                self.assertEqual(contract.execution_mode, mode)


if __name__ == "__main__":
    unittest.main(verbosity=2)
