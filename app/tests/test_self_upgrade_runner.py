from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from core import authorized_upgrade, owner_approval, permissions

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "agenelf_self_upgrade_runner",
    PROJECT_ROOT / "scripts" / "self_upgrade_runner.py",
)
runner = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


BASE_MODULE = "def value():\n    return 1\n"
CANDIDATE_MODULE = "def value():\n    return 2\n"
EXISTING_TEST = '''import unittest
from core.example import value

class ExistingTest(unittest.TestCase):
    def test_returns_an_integer(self):
        self.assertIsInstance(value(), int)

if __name__ == "__main__":
    unittest.main()
'''
NEW_TEST = '''import unittest
from core.example import value

class UpgradeTest(unittest.TestCase):
    def test_upgraded_value(self):
        self.assertEqual(value(), 2)

if __name__ == "__main__":
    unittest.main()
'''


class SelfUpgradeRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_root = os.environ.get("AGENELF_ROOT")
        os.environ["AGENELF_ROOT"] = str(self.root)

        runner.ROOT = self.root
        runner.APP_DIR = self.root / "app-fork"
        runner.TARGET_ROOT = self.root / "upgrade-target"
        runner.CANDIDATE_REPO = self.root / "app-tmp" / "repo"

        (self.root / "scripts").mkdir(parents=True)
        shutil.copy2(
            PROJECT_ROOT / "scripts" / "run_authorized_upgrade_tests.py",
            self.root / "scripts" / "run_authorized_upgrade_tests.py",
        )
        for base in (runner.CANDIDATE_REPO, runner.TARGET_ROOT):
            (base / "app" / "core").mkdir(parents=True)
            (base / "app" / "tests").mkdir(parents=True)
            (base / "app" / "core" / "__init__.py").write_text("", encoding="utf-8")
            (base / "app" / "tests" / "__init__.py").write_text("", encoding="utf-8")
            (base / "app" / "tests" / "test_existing.py").write_text(
                EXISTING_TEST,
                encoding="utf-8",
            )

        (runner.TARGET_ROOT / "app" / "core" / "example.py").write_text(
            BASE_MODULE,
            encoding="utf-8",
        )
        (runner.CANDIDATE_REPO / "app" / "core" / "example.py").write_text(
            CANDIDATE_MODULE,
            encoding="utf-8",
        )
        (runner.CANDIDATE_REPO / "app" / "tests" / "test_upgrade.py").write_text(
            NEW_TEST,
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        if self.old_root is None:
            os.environ.pop("AGENELF_ROOT", None)
        else:
            os.environ["AGENELF_ROOT"] = self.old_root
        self.tmp.cleanup()

    def _prepare_request(self) -> tuple[dict, dict]:
        session_id = "upgrade-20260726-120000-12345678"
        baseline_manifest = {
            "app/core/__init__.py": runner.file_sha256(
                runner.TARGET_ROOT / "app" / "core" / "__init__.py"
            ),
            "app/core/example.py": runner.file_sha256(
                runner.TARGET_ROOT / "app" / "core" / "example.py"
            ),
            "app/tests/__init__.py": runner.file_sha256(
                runner.TARGET_ROOT / "app" / "tests" / "__init__.py"
            ),
            "app/tests/test_existing.py": runner.file_sha256(
                runner.TARGET_ROOT / "app" / "tests" / "test_existing.py"
            ),
        }
        baseline_path = (
            self.root
            / "data"
            / "authorized-upgrades"
            / session_id
            / "baseline-manifest.json"
        )
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps(baseline_manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        records = [
            {
                "path": "app/core/example.py",
                "before_sha256": baseline_manifest["app/core/example.py"],
                "after_sha256": runner.file_sha256(
                    runner.CANDIDATE_REPO / "app" / "core" / "example.py"
                ),
                "changed_lines": 2,
                "created": False,
            },
            {
                "path": "app/tests/test_upgrade.py",
                "before_sha256": "",
                "after_sha256": runner.file_sha256(
                    runner.CANDIDATE_REPO / "app" / "tests" / "test_upgrade.py"
                ),
                "changed_lines": len(NEW_TEST.splitlines()),
                "created": True,
            },
        ]
        candidate_manifest = runner.tree_manifest(runner.CANDIDATE_REPO)
        candidate_digest = runner.json_digest(candidate_manifest)
        binding = {
            "schema_version": 1,
            "kind": "owner_authorized_self_upgrade_candidate",
            "session_id": session_id,
            "intent_auth_id": "auth-111111111111",
            "goal_sha256": "a" * 64,
            "scopes": ["app_runtime", "tests"],
            "allowed_paths": ["app/core/", "app/tests/"],
            "changed_files": records,
            "candidate_tree_sha256": candidate_digest,
            "test_report_sha256": "b" * 64,
            "baseline_manifest_sha256": runner.file_sha256(baseline_path),
        }
        ok, candidate_auth_id = permissions.request_auth(
            "authorized_self_upgrade",
            "approve_tested_candidate",
            "approve exact test candidate",
            binding=binding,
            operation="owner_authorized_self_upgrade_candidate",
            capability="agent.authorized_self_upgrade",
        )
        self.assertTrue(ok)
        owner_approval.apply_owner_decision(
            candidate_auth_id,
            "approve",
            decided_by="owner-test",
            root=self.root,
        )

        session = {
            "schema_version": 1,
            "id": session_id,
            "status": "apply_queued",
            "goal": "upgrade example",
            "plan": {
                "goal_sha256": "a" * 64,
                "scopes": ["app_runtime", "tests"],
                "allowed_paths": ["app/core/", "app/tests/"],
            },
            "intent_auth_id": "auth-111111111111",
            "intent_consumed": True,
            "candidate_auth_id": candidate_auth_id,
            "candidate_binding": binding,
            "candidate_digest": candidate_digest,
            "changed_file_records": records,
            "baseline_manifest_path": str(baseline_path),
        }
        authorized_upgrade.save_session(session, self.root)

        request_id = "self-upgrade-1234567890abcdef"
        payload = {
            "schema_version": 1,
            "session_id": session_id,
            "intent_auth_id": session["intent_auth_id"],
            "candidate_auth_id": candidate_auth_id,
            "candidate_binding": binding,
            "candidate_digest": candidate_digest,
            "changed_files": records,
            "candidate_repo": str(runner.CANDIDATE_REPO),
        }
        request = {
            "id": request_id,
            "created_at": authorized_upgrade.now_iso(),
            **payload,
            "fingerprint": runner.json_digest(payload),
        }
        path = self.root / "data" / "self-upgrade-requests" / f"{request_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
        return request, session

    def test_exact_candidate_is_retested_backed_up_and_applied(self) -> None:
        request, session = self._prepare_request()
        state = runner.process_request(
            self.root / "data" / "self-upgrade-requests" / f"{request['id']}.json"
        )
        self.assertEqual(state, "succeeded")
        self.assertEqual(
            (runner.TARGET_ROOT / "app" / "core" / "example.py").read_text(
                encoding="utf-8"
            ),
            CANDIDATE_MODULE,
        )
        self.assertTrue(
            (runner.TARGET_ROOT / "app" / "tests" / "test_upgrade.py").is_file()
        )
        result = json.loads(
            (
                self.root
                / "data"
                / "self-upgrade-results"
                / f"{request['id']}.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(Path(result["backup_dir"]).is_dir())
        self.assertTrue(result["restart_required"])
        self.assertEqual(
            permissions.check_auth(session["candidate_auth_id"]),
            permissions.STATUS_USED,
        )

    def test_stale_target_hash_is_rejected_before_write(self) -> None:
        request, _session = self._prepare_request()
        target = runner.TARGET_ROOT / "app" / "core" / "example.py"
        target.write_text("def value():\n    return 999\n", encoding="utf-8")
        state = runner.process_request(
            self.root / "data" / "self-upgrade-requests" / f"{request['id']}.json"
        )
        self.assertEqual(state, "failed")
        self.assertIn("return 999", target.read_text(encoding="utf-8"))
        result = json.loads(
            (
                self.root
                / "data"
                / "self-upgrade-results"
                / f"{request['id']}.json"
            ).read_text(encoding="utf-8")
        )
        self.assertIn("stale overwrite", result["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
