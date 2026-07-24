from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from core import operations


class ApproveScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "scripts").mkdir()
        source = Path(__file__).resolve().parents[2] / "scripts" / "approve.sh"
        shutil.copy(source, self.root / "scripts" / "approve.sh")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, *args):
        return subprocess.run(
            ["bash", "scripts/approve.sh", *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def test_real_script_creates_exact_decision_once(self):
        request = operations.submit_operation(
            "server.operations",
            "apt_update",
            "primary",
            {},
            "change",
            "apt",
            root=self.root,
        )
        result = self._run(request["id"], "approve")
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        decision = json.loads(
            (self.root / "data" / "auth-decisions" / f"{request['id']}.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(decision["fingerprint"], request["fingerprint"])
        self.assertEqual(decision["decision"], "approve")
        again = self._run(request["id"], "approve")
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("不允许覆盖", again.stderr)

    def test_tampered_request_cannot_be_approved(self):
        request = operations.submit_operation(
            "server.operations",
            "apt_update",
            "primary",
            {},
            "change",
            "apt",
            root=self.root,
        )
        path = self.root / "data" / "ops-requests" / f"{request['id']}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["target"] = "secondary"
        path.write_text(json.dumps(data), encoding="utf-8")
        result = self._run(request["id"], "approve")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("指纹不匹配", result.stderr + result.stdout)
        self.assertFalse(
            (self.root / "data" / "auth-decisions" / f"{request['id']}.json").exists()
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
