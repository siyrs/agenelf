from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.local_context import LocalContextStore


class LocalContextStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "context").mkdir()
        (self.root / "profile.yaml").write_text(
            "owner:\n  name: Sirius\n  api_key: sk-abcdefgh12345678\n",
            encoding="utf-8",
        )
        (self.root / "preferences.yaml").write_text(
            "hobbies:\n  - Android\n  - 本地 AI\n",
            encoding="utf-8",
        )
        (self.root / "context" / "projects.md").write_text(
            "当前项目是 Agenelf，password=do-not-store",
            encoding="utf-8",
        )
        (self.root / "servers.yaml").write_text(
            """servers:
  primary:
    host: 10.0.0.8
    username: root
    auth:
      private_key: secret.pem
    allowed_operations: [inspect, docker_ps]
    allowed_services: [docker]
""",
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_prompt_contains_personalization_but_not_secrets_or_hosts(self):
        store = LocalContextStore(self.root)
        prompt = store.prompt_block()
        self.assertIn("Sirius", prompt)
        self.assertIn("Android", prompt)
        self.assertIn("primary", prompt)
        self.assertNotIn("sk-abcdefgh", prompt)
        self.assertNotIn("do-not-store", prompt)
        self.assertNotIn("10.0.0.8", prompt)
        self.assertNotIn("secret.pem", prompt)
        self.assertTrue(store.status()["warnings"])
        self.assertFalse(store.status()["secrets_visible_to_agent"])

    def test_fingerprint_changes_when_safe_context_changes(self):
        store = LocalContextStore(self.root)
        before = store.fingerprint
        (self.root / "preferences.yaml").write_text(
            "hobbies:\n  - 树莓派\n", encoding="utf-8"
        )
        store.reload()
        self.assertNotEqual(before, store.fingerprint)


if __name__ == "__main__":
    unittest.main(verbosity=2)
