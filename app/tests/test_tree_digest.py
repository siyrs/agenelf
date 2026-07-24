"""Trusted candidate tree digest tests."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "tree_digest.py"
SPEC = importlib.util.spec_from_file_location("trusted_tree_digest", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class TreeDigestTest(unittest.TestCase):
    def test_digest_is_stable_and_content_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a").mkdir()
            (root / "a" / "x.py").write_text("X = 1\n", encoding="utf-8")
            first = MODULE.tree_digest(root)
            second = MODULE.tree_digest(root)
            self.assertEqual(first, second)
            (root / "a" / "x.py").write_text("X = 2\n", encoding="utf-8")
            self.assertNotEqual(first, MODULE.tree_digest(root))

    def test_transient_python_cache_is_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "x.py").write_text("X = 1\n", encoding="utf-8")
            before = MODULE.tree_digest(root)
            cache = root / "__pycache__"
            cache.mkdir()
            (cache / "x.cpython-312.pyc").write_bytes(b"noise")
            self.assertEqual(before, MODULE.tree_digest(root))

    def test_relative_path_is_part_of_digest(self):
        with tempfile.TemporaryDirectory() as first_tmp, tempfile.TemporaryDirectory() as second_tmp:
            first = Path(first_tmp)
            second = Path(second_tmp)
            (first / "a.txt").write_text("same", encoding="utf-8")
            (second / "b.txt").write_text("same", encoding="utf-8")
            self.assertNotEqual(MODULE.tree_digest(first), MODULE.tree_digest(second))


if __name__ == "__main__":
    unittest.main(verbosity=2)
