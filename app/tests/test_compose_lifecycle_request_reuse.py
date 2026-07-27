from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path

from skills import compose_lifecycle


class ComposeLifecycleRequestReuseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_root = os.environ.get("AGENELF_ROOT")
        self.old_servers = os.environ.get("AGENELF_SERVERS_FILE")
        os.environ["AGENELF_ROOT"] = str(self.root)
        servers = self.root / "servers.yaml"
        servers.write_text(
            """servers:
  primary:
    host: 127.0.0.1
    username: agenelf
    managed_root: /srv/agenelf
    allowed_operations: [compose_deploy]
""",
            encoding="utf-8",
        )
        os.environ["AGENELF_SERVERS_FILE"] = str(servers)

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
    def _operation_id(text: str) -> str:
        match = re.search(r"op-[0-9a-f]{16}", text)
        if match is None:
            raise AssertionError(text)
        return match.group(0)

    def test_identical_change_reuses_one_request_and_has_no_bash_only_instruction(self) -> None:
        first = compose_lifecycle.down_compose_project(
            "primary",
            "vpn",
            timeout_seconds=30,
            remove_orphans=True,
        )
        second = compose_lifecycle.down_compose_project(
            "primary",
            "vpn",
            timeout_seconds=30,
            remove_orphans=True,
        )

        self.assertEqual(self._operation_id(first), self._operation_id(second))
        self.assertIn("已复用相同未完成", second)
        self.assertIn("/approve op-", first)
        self.assertIn("审批通过 op-", first)
        self.assertIn("approve.ps1", first)
        self.assertIn("scripts/approve.py", first)
        self.assertNotIn("bash scripts/approve.sh", first)
        self.assertEqual(
            len(list((self.root / "data" / "ops-requests").glob("op-*.json"))),
            1,
        )

    def test_parameter_change_creates_a_new_request(self) -> None:
        first = compose_lifecycle.down_compose_project(
            "primary",
            "vpn",
            timeout_seconds=30,
        )
        second = compose_lifecycle.down_compose_project(
            "primary",
            "vpn",
            timeout_seconds=45,
        )
        self.assertNotEqual(self._operation_id(first), self._operation_id(second))


if __name__ == "__main__":
    unittest.main(verbosity=2)
