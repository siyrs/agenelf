"""Regression tests for the server capability's no-arbitrary-shell boundary."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from skills import server_ops


class ServerOpsSafetyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_root = os.environ.get("AGENELF_ROOT")
        self.old_servers = os.environ.get("AGENELF_SERVERS_FILE")
        os.environ["AGENELF_ROOT"] = str(self.root)
        server_file = self.root / "servers.yaml"
        server_file.write_text(
            """servers:
  primary:
    host: 127.0.0.1
    username: agenelf
    managed_root: /srv/agenelf
    allowed_operations: [inspect, docker_ps, service_status, apt_update, compose_deploy, service_restart, docker_install]
    allowed_services: [nginx]
    allowed_bind_roots: [/srv/data]
""",
            encoding="utf-8",
        )
        os.environ["AGENELF_SERVERS_FILE"] = str(server_file)

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

    def test_arbitrary_local_shell_cannot_be_confirmed(self):
        victim = self.root / "victim"
        result = server_ops.run_shell(f"mkdir {victim}", confirm=True)
        self.assertIn("通用 shell 执行已关闭", result)
        self.assertFalse(victim.exists())

    def test_change_creates_bound_operation_request(self):
        result = server_ops.update_apt_index("primary")
        self.assertIn("运维请求已创建：op-", result)
        self.assertIn("scripts/approve.sh", result)
        requests = list((self.root / "data" / "ops-requests").glob("op-*.json"))
        self.assertEqual(len(requests), 1)
        self.assertIn('"operation": "apt_update"', requests[0].read_text(encoding="utf-8"))

    def test_compose_red_lines(self):
        docker_socket = """services:
  api:
    image: nginx
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
"""
        result = server_ops.deploy_compose_project(
            "primary", "api", docker_socket, plan_only=True
        )
        self.assertIn("安全校验失败", result)
        self.assertIn("Docker Socket", result)

        privileged = """services:
  api:
    image: nginx
    privileged: true
"""
        result = server_ops.deploy_compose_project(
            "primary", "api", privileged, plan_only=True
        )
        self.assertIn("privileged", result)

    def test_valid_plan_does_not_submit(self):
        compose = """services:
  web:
    image: nginx:alpine
    ports: ["8080:80"]
"""
        result = server_ops.deploy_compose_project(
            "primary", "demo", compose, plan_only=True
        )
        self.assertIn("计划校验通过", result)
        self.assertFalse((self.root / "data" / "ops-requests").exists())

    def test_service_allowlist(self):
        result = server_ops.manage_system_service("primary", "ssh", "restart")
        self.assertIn("不在 allowed_services", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
