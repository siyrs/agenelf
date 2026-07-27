from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path

from skills import docker_ops, server_ops


class CrossPlatformOperationGuidanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_root = os.environ.get("AGENELF_ROOT")
        self.old_servers = os.environ.get("AGENELF_SERVERS_FILE")
        os.environ["AGENELF_ROOT"] = str(self.root)
        self.servers = self.root / "servers.yaml"
        self.servers.write_text(
            """servers:
  primary:
    host: 127.0.0.1
    username: agenelf
    managed_root: /srv/agenelf
    allowed_operations: [apt_update, compose_deploy, service_restart]
    allowed_services: [nginx]
    allowed_docker_operations: [restart_docker_container]
    allowed_containers: [sing-box]
""",
            encoding="utf-8",
        )
        os.environ["AGENELF_SERVERS_FILE"] = str(self.servers)

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

    @staticmethod
    def _id(text: str) -> str:
        match = re.search(r"op-[0-9a-f]{16}", text)
        if match is None:
            raise AssertionError(text)
        return match.group(0)

    def _assert_cross_platform(self, text: str) -> None:
        self.assertIn("/approve op-", text)
        self.assertIn("审批通过 op-", text)
        self.assertIn("approve.ps1", text)
        self.assertIn("scripts/approve.py", text)
        self.assertNotIn("bash scripts/approve.sh", text)
        self.assertIn("请求有效期至", text)

    def test_server_change_uses_cross_platform_guidance_and_reuses_request(self) -> None:
        first = server_ops.update_apt_index("primary")
        second = server_ops.update_apt_index("primary")

        self._assert_cross_platform(first)
        self.assertEqual(self._id(first), self._id(second))
        self.assertIn("已复用相同未完成", second)

    def test_docker_restart_uses_cross_platform_guidance_and_reuses_request(self) -> None:
        first = docker_ops.restart_docker_container("primary", "sing-box", 10)
        second = docker_ops.restart_docker_container("primary", "sing-box", 10)

        self._assert_cross_platform(first)
        self.assertEqual(self._id(first), self._id(second))
        self.assertIn("已复用相同未完成", second)

    def test_parameter_change_requires_new_exact_request(self) -> None:
        first = docker_ops.restart_docker_container("primary", "sing-box", 10)
        second = docker_ops.restart_docker_container("primary", "sing-box", 20)
        self.assertNotEqual(self._id(first), self._id(second))

    def test_compose_security_validation_is_unchanged(self) -> None:
        rejected = server_ops.deploy_compose_project(
            "primary",
            "unsafe",
            "services:\n  bad:\n    image: busybox\n    privileged: true\n",
        )
        self.assertIn("禁止 privileged=true", rejected)
        self.assertFalse((self.root / "data" / "ops-requests").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
