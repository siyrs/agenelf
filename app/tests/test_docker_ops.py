from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from skills import docker_ops


class DockerOpsSkillTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_root = os.environ.get("AGENELF_ROOT")
        self.old_servers = os.environ.get("AGENELF_SERVERS_FILE")
        os.environ["AGENELF_ROOT"] = str(self.root)
        self.servers = self.root / "servers.yaml"
        os.environ["AGENELF_SERVERS_FILE"] = str(self.servers)
        self.servers.write_text(
            """servers:
  pve-ubuntu:
    host: 192.0.2.20
    username: sirius
    docker_command: docker
    allowed_docker_operations:
      - get_docker_logs
      - inspect_docker_container
      - run_docker_check
      - restart_docker_container
    allowed_containers: [sing-box]
    docker_checks:
      sing-box-config:
        container: sing-box
        argv: [sing-box, check, -c, /etc/sing-box/config.json]
""",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        if self.old_root is None:
            os.environ.pop("AGENELF_ROOT", None)
        else:
            os.environ["AGENELF_ROOT"] = self.old_root
        if self.old_servers is None:
            os.environ.pop("AGENELF_SERVERS_FILE", None)
        else:
            os.environ["AGENELF_SERVERS_FILE"] = self.old_servers
        self.tmp.cleanup()

    def _requests(self) -> list[Path]:
        return list((self.root / "data" / "ops-requests").glob("op-*.json"))

    def test_logs_create_read_only_fingerprint_bound_request(self) -> None:
        result = docker_ops.get_docker_logs(
            "pve-ubuntu", "sing-box", tail=120, wait_seconds=0
        )
        state = json.loads(result)
        self.assertEqual(state["status"], "queued")
        request = json.loads(self._requests()[0].read_text(encoding="utf-8"))
        self.assertEqual(request["capability"], "docker.operations")
        self.assertEqual(request["operation"], "get_docker_logs")
        self.assertEqual(request["risk"], "read")
        self.assertEqual(request["parameters"], {"container": "sing-box", "tail": 120})
        self.assertRegex(request["fingerprint"], r"^[0-9a-f]{64}$")

    def test_restart_requires_exact_host_approval(self) -> None:
        result = docker_ops.restart_docker_container(
            "pve-ubuntu", "sing-box", timeout_seconds=15
        )
        self.assertIn("Docker 运维请求已创建：op-", result)
        self.assertIn("scripts/approve.sh", result)
        request = json.loads(self._requests()[0].read_text(encoding="utf-8"))
        self.assertEqual(request["risk"], "change")
        self.assertEqual(request["parameters"]["timeout_seconds"], 15)

    def test_container_allowlist_and_name_validation(self) -> None:
        denied = docker_ops.get_docker_logs("pve-ubuntu", "gitlab", wait_seconds=0)
        self.assertIn("allowed_containers", denied)
        invalid = docker_ops.get_docker_logs(
            "pve-ubuntu", "sing-box; id", wait_seconds=0
        )
        self.assertIn("名称非法", invalid)
        self.assertFalse(self._requests())

    def test_check_is_selected_by_alias_not_model_supplied_command(self) -> None:
        result = docker_ops.run_docker_check(
            "pve-ubuntu", "sing-box-config", wait_seconds=0
        )
        state = json.loads(result)
        self.assertEqual(state["status"], "queued")
        request = json.loads(self._requests()[0].read_text(encoding="utf-8"))
        self.assertEqual(request["parameters"], {"check": "sing-box-config"})
        self.assertNotIn("argv", request["parameters"])

    def test_runtime_summary_hides_diagnostic_argv(self) -> None:
        result = json.loads(docker_ops.list_docker_runtime("pve-ubuntu"))
        self.assertEqual(result["docker_checks"], [
            {"name": "sing-box-config", "container": "sing-box"}
        ])
        self.assertNotIn("/etc/sing-box/config.json", json.dumps(result))


if __name__ == "__main__":
    unittest.main(verbosity=2)
