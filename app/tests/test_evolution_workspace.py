from __future__ import annotations

import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from core.evolution_workspace import (
    EvolutionWorkspaceError,
    assert_trusted_tests_unchanged,
    candidate_app,
    clear_tree_contents,
    stage_workspace,
)
from skills import evolution_ops

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "candidate_runner_under_test", PROJECT_ROOT / "scripts" / "run_candidate_tests.py"
)
candidate_runner = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(candidate_runner)

BASE_CODE = "def answer():\n    return 41\n"
BASE_TEST = '''import unittest
from core.example import answer

class T(unittest.TestCase):
    def test_answer(self):
        self.assertEqual(answer(), 41)

if __name__ == "__main__":
    unittest.main()
'''
NEW_TEST = '''import unittest
from core.example import answer

class Added(unittest.TestCase):
    def test_answer_is_positive(self):
        self.assertGreater(answer(), 0)

if __name__ == "__main__":
    unittest.main()
'''


class EvolutionWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for path in (
            self.root / "app-fork" / "core",
            self.root / "app-fork" / "tests",
            self.root / "repo-source" / ".github" / "workflows",
            self.root / "scripts",
            self.root / "policy",
            self.root / "app-tmp",
            self.root / "data",
        ):
            path.mkdir(parents=True, exist_ok=True)
        (self.root / "app-fork" / "core" / "__init__.py").write_text("", encoding="utf-8")
        (self.root / "app-fork" / "core" / "example.py").write_text(BASE_CODE, encoding="utf-8")
        (self.root / "app-fork" / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (self.root / "app-fork" / "tests" / "test_example.py").write_text(BASE_TEST, encoding="utf-8")
        (self.root / "repo-source" / ".github" / "workflows" / "ci.yml").write_text(
            "name: CI\n", encoding="utf-8"
        )
        (self.root / "repo-source" / "docker-compose.yml").write_text(
            "services: {}\n", encoding="utf-8"
        )
        shutil.copy2(
            PROJECT_ROOT / "scripts" / "run_candidate_tests.py",
            self.root / "scripts" / "run_candidate_tests.py",
        )
        self.old_root = os.environ.get("AGENELF_ROOT")
        os.environ["AGENELF_ROOT"] = str(self.root)

    def tearDown(self):
        if self.old_root is None:
            os.environ.pop("AGENELF_ROOT", None)
        else:
            os.environ["AGENELF_ROOT"] = self.old_root
        self.tmp.cleanup()

    def test_clear_preserves_mount_root_and_removes_stale_contents(self):
        mount = self.root / "app-tmp"
        (mount / "stale" / "nested").mkdir(parents=True)
        (mount / "stale" / "nested" / "x.txt").write_text("x", encoding="utf-8")
        clear_tree_contents(mount)
        self.assertTrue(mount.is_dir())
        self.assertEqual(list(mount.iterdir()), [])

    def test_stage_creates_repository_shape_with_safe_fixtures(self):
        marker = stage_workspace(self.root, self.root / "app-fork")
        app = self.root / "app-tmp" / "repo" / "app"
        repo = app.parent
        self.assertEqual(marker["layout"], "repository")
        self.assertTrue((app / "core" / "example.py").is_file())
        self.assertTrue((repo / ".github" / "workflows" / "ci.yml").is_file())
        self.assertTrue((repo / "docker-compose.yml").is_file())
        self.assertTrue((repo / "scripts" / "run_candidate_tests.py").is_file())
        self.assertNotIn("local/secrets", json.dumps(marker))

    def test_candidate_runner_separates_new_tests_and_rejects_tampering(self):
        marker = stage_workspace(self.root, self.root / "app-fork")
        app = Path(marker["candidate_app"])
        (app / "tests" / "test_added.py").write_text(NEW_TEST, encoding="utf-8")
        code, result = candidate_runner.evaluate(
            self.root / "app-fork", app, phase="candidate", timeout=60
        )
        self.assertEqual(code, 0, result)
        self.assertEqual(result["baseline_count"], 1)
        self.assertEqual(result["new_test_count"], 1)

        (app / "tests" / "test_example.py").write_text(
            "import unittest\n", encoding="utf-8"
        )
        code, result = candidate_runner.evaluate(
            self.root / "app-fork", app, phase="candidate", timeout=60
        )
        self.assertEqual(code, candidate_runner.EXIT_BASELINE_TAMPERED)
        self.assertEqual(result["status"], "baseline_tests_tampered")
        self.assertIn("tests/test_example.py", result["changed"])

    def test_candidate_runner_uses_minimal_environment_allowlist(self):
        secret_name = "AGENELF_TEST_SECRET_TOKEN"
        old = os.environ.get(secret_name)
        os.environ[secret_name] = "super-secret"
        try:
            env = candidate_runner._environment(self.root / "app-tmp")
        finally:
            if old is None:
                os.environ.pop(secret_name, None)
            else:
                os.environ[secret_name] = old
        self.assertNotIn(secret_name, env)
        for leaked in ("AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "OPENAI_API_KEY"):
            self.assertNotIn(leaked, env)
        self.assertIn("PATH", env)
        self.assertIn("PYTHONPATH", env)
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertIn(str(self.root / "app-tmp"), env["PYTHONPATH"])

    def test_helper_detects_direct_test_mutation(self):
        marker = stage_workspace(self.root, self.root / "app-fork")
        app = Path(marker["candidate_app"])
        (app / "tests" / "test_example.py").write_text("broken", encoding="utf-8")
        with self.assertRaises(EvolutionWorkspaceError):
            assert_trusted_tests_unchanged(app, marker["baseline_tests"])

    def test_evolution_begin_preflights_before_editing(self):
        result = evolution_ops.evolution_begin("新增一个不破坏基线的技能")
        self.assertIn("基线", result)
        state = json.loads(
            (self.root / "data" / "evolution-session.json").read_text(encoding="utf-8")
        )
        self.assertEqual(state["status"], "editing")
        self.assertEqual(state["layout"], "repository")
        self.assertEqual(Path(state["candidate_app"]), candidate_app(self.root))
        self.assertEqual(state["baseline_preflight"]["exit_code"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
