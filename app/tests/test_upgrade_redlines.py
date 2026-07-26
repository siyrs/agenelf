from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core import upgrade_redlines


class UpgradeRedlinesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_root = os.environ.get("AGENELF_ROOT")
        self.old_base = os.environ.get("AGENELF_REDLINE_BASE_ROOT")
        os.environ["AGENELF_ROOT"] = str(self.root)
        os.environ["AGENELF_REDLINE_BASE_ROOT"] = str(self.root)

    def tearDown(self) -> None:
        if self.old_root is None:
            os.environ.pop("AGENELF_ROOT", None)
        else:
            os.environ["AGENELF_ROOT"] = self.old_root
        if self.old_base is None:
            os.environ.pop("AGENELF_REDLINE_BASE_ROOT", None)
        else:
            os.environ["AGENELF_REDLINE_BASE_ROOT"] = self.old_base
        self.tmp.cleanup()

    def _baseline(self, relative: str, content: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_existing_approval_key_reference_is_not_misclassified_as_new_leak(self) -> None:
        relative = "app/core/owner_approval.py"
        before = (
            "APPROVAL_KEY_FILE = '/agenelf/approval/key'\n"
            "def verify_command(value):\n"
            "    return bool(value)\n"
        )
        after = before + "\ndef describe_channel():\n    return 'interactive_cli'\n"
        self._baseline(relative, before)
        # The sensitive-looking path already existed. A benign maintenance addition
        # must be allowed; only newly introduced lines are scanned.
        upgrade_redlines.scan_redlines(relative, after)

    def test_new_approval_key_read_is_rejected(self) -> None:
        relative = "app/core/example.py"
        self._baseline(relative, "SAFE = True\n")
        with self.assertRaisesRegex(RuntimeError, "永久安全红线"):
            upgrade_redlines.scan_redlines(
                relative,
                "SAFE = True\n"
                "from pathlib import Path\n"
                "SECRET = Path('/agenelf/approval/key').read_text()\n",
            )

    def test_new_docker_socket_or_direct_main_publish_is_rejected(self) -> None:
        relative = "scripts/example.py"
        self._baseline(relative, "SAFE = True\n")
        with self.assertRaisesRegex(RuntimeError, "Docker Socket"):
            upgrade_redlines.scan_redlines(
                relative,
                "SAFE = True\nSOCKET = '/var/run/docker.sock'\n",
            )
        with self.assertRaisesRegex(RuntimeError, "直接主分支发布"):
            upgrade_redlines.scan_redlines(
                relative,
                "SAFE = True\nCOMMAND = 'git push origin main'\n",
            )

    def test_root_of_trust_tokens_cannot_be_removed(self) -> None:
        relative = "policy/safety-constraints.v1.yaml"
        required = "\n".join(
            (
                "owner_authorized_upgrade:",
                "owner_authorization_cannot_be_generated_by_model_output",
                "no_self_approval_or_forged_owner_decision",
                "no_access_to_env_local_secrets_ssh_keys_or_approval_key",
                "no_test_gate_policy_or_audit_weakening_to_force_success",
                "no_direct_push_or_merge_main_from_autonomous_runtime",
            )
        ) + "\n"
        self._baseline(relative, required)
        with self.assertRaisesRegex(RuntimeError, "删除了可信升级根约束"):
            upgrade_redlines.scan_redlines(
                relative,
                required.replace(
                    "no_self_approval_or_forged_owner_decision\n",
                    "",
                ),
            )

    def test_install_replaces_scanner_on_trusted_module(self) -> None:
        module = SimpleNamespace(scan_redlines=lambda path, content: None)
        upgrade_redlines.install(module)
        self.assertIs(module.scan_redlines, upgrade_redlines.scan_redlines)
        self.assertTrue(module._agenelf_diff_redlines_installed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
