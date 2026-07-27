from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from core.storage import atomic_write_json, now_iso, read_json, safe_text


class NowIsoTest(unittest.TestCase):
    def test_returns_utc_iso_with_second_precision(self):
        value = now_iso()
        parsed = datetime.fromisoformat(value)
        self.assertIsNotNone(parsed.tzinfo)
        self.assertEqual(parsed.utcoffset(), datetime.now().astimezone().utcoffset() * 0)
        self.assertEqual(parsed.microsecond, 0)
        self.assertNotIn(".", value)

    def test_two_calls_are_close_in_time(self):
        first = datetime.fromisoformat(now_iso())
        second = datetime.fromisoformat(now_iso())
        self.assertLessEqual(first, second)
        self.assertLess((second - first).total_seconds(), 5)


class ReadJsonTest(unittest.TestCase):
    def test_reads_valid_json_as_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "value.json"
            path.write_text(json.dumps({"a": [1, 2]}), encoding="utf-8")
            self.assertEqual(read_json(path), {"a": [1, 2]})

    def test_returns_non_dict_values_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "list.json"
            path.write_text("[1, 2, 3]", encoding="utf-8")
            self.assertEqual(read_json(path), [1, 2, 3])

    def test_missing_file_returns_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.json"
            self.assertIsNone(read_json(path))
            self.assertEqual(read_json(path, {}), {})
            self.assertEqual(read_json(path, []), [])

    def test_corrupt_json_returns_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text("{not valid json", encoding="utf-8")
            self.assertIsNone(read_json(path))
            self.assertEqual(read_json(path, {"fallback": True}), {"fallback": True})

    def test_unreadable_path_returns_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "a-dir"
            directory.mkdir()
            self.assertIsNone(read_json(directory))


class AtomicWriteJsonTest(unittest.TestCase):
    def test_writes_pretty_utf8_json_with_trailing_newline(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.json"
            atomic_write_json(path, {"键": "值", "n": 1})
            raw = path.read_text(encoding="utf-8")
            self.assertTrue(raw.endswith("\n"))
            self.assertIn("键", raw)  # ensure_ascii=False
            self.assertIn("\n  ", raw)  # indent=2
            self.assertEqual(json.loads(raw), {"键": "值", "n": 1})

    def test_creates_missing_parent_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deep" / "nested" / "out.json"
            atomic_write_json(path, {"ok": True})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"ok": True})

    def test_overwrites_existing_file_atomically_without_tmp_residue(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.json"
            atomic_write_json(path, {"version": 1})
            atomic_write_json(path, {"version": 2})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"version": 2})
            leftovers = [p.name for p in Path(tmp).iterdir() if p.name != "out.json"]
            self.assertEqual(leftovers, [])

    def test_exclusive_creates_new_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "once.json"
            atomic_write_json(path, {"created": True}, exclusive=True)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")), {"created": True}
            )
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_exclusive_does_not_overwrite_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "once.json"
            atomic_write_json(path, {"original": True})
            with self.assertRaises(FileExistsError):
                atomic_write_json(path, {"original": False}, exclusive=True)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")), {"original": True}
            )

    def test_failed_write_leaves_no_tmp_residue_and_preserves_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.json"
            atomic_write_json(path, {"good": True})

            class Unserializable:
                pass

            with self.assertRaises(TypeError):
                atomic_write_json(path, {"bad": Unserializable()})
            # 序列化在写临时文件之前完成，目标文件未被触碰，也无残留
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"good": True})
            leftovers = [p.name for p in Path(tmp).iterdir() if p.name != "out.json"]
            self.assertEqual(leftovers, [])


class SafeTextTest(unittest.TestCase):
    def test_strips_whitespace(self):
        self.assertEqual(safe_text("  hello \n"), "hello")

    def test_none_and_falsy_become_empty(self):
        self.assertEqual(safe_text(None), "")
        self.assertEqual(safe_text(""), "")

    def test_coerces_non_string_values(self):
        self.assertEqual(safe_text(42), "42")

    def test_truncates_with_ellipsis(self):
        result = safe_text("x" * 3000)
        self.assertEqual(len(result), 2000)
        self.assertTrue(result.endswith("…"))

    def test_respects_custom_limit(self):
        self.assertEqual(safe_text("abcdef", 4), "abc…")
        self.assertEqual(safe_text("abc", 4), "abc")

    def test_limit_one_yields_only_ellipsis(self):
        self.assertEqual(safe_text("abcdef", 1), "…")

    def test_multiline_text_is_preserved(self):
        self.assertEqual(safe_text("a\nb"), "a\nb")


if __name__ == "__main__":
    unittest.main()
