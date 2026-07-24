from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.memory import MemoryStore


class MemoryPrivacyTest(unittest.TestCase):
    def test_memory_is_redacted_deduplicated_and_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memory" / "memory.json"
            store = MemoryStore(str(path), max_entries=2)
            self.assertTrue(store.add("fact", "API 是 sk-abcdefgh12345678"))
            self.assertFalse(store.add("fact", "API 是 sk-abcdefgh12345678"))
            store.add("preference", "喜欢 Android")
            store.add("episode", "第三条")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(data), 2)
            serialized = json.dumps(data, ensure_ascii=False)
            self.assertNotIn("sk-abcdefgh", serialized)
            self.assertEqual(store.stats()["entries"], 2)

    def test_prompt_redacts_legacy_untrusted_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memory.json"
            path.write_text(
                json.dumps(
                    [{"kind": "fact", "content": "token=abcdefghi123456", "ts": 1}]
                ),
                encoding="utf-8",
            )
            store = MemoryStore(str(path))
            prompt = store.as_prompt_block()
            self.assertNotIn("abcdefghi123456", prompt)
            self.assertIn("[REDACTED]", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
