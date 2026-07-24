"""长期记忆的持久化和提示词限额测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.memory import MemoryStore


class MemoryStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "memory.json"
        self.store = MemoryStore(str(self.path))

    def test_save_is_reloadable_json(self):
        self.store.add("fact", "使用 UTF-8 保存")
        self.assertEqual(
            json.loads(self.path.read_text(encoding="utf-8"))[0]["content"],
            "使用 UTF-8 保存",
        )
        reloaded = MemoryStore(str(self.path))
        self.assertEqual(reloaded.recall("UTF-8"), ["使用 UTF-8 保存"])

    def test_prompt_uses_newest_entries_and_stays_bounded(self):
        self.store.memories = [
            {"kind": "fact", "content": "旧记忆", "ts": 1},
            {"kind": "preference", "content": "新记忆", "ts": 2},
            {"kind": "episode", "content": "最新记忆", "ts": 3},
        ]
        block = self.store.as_prompt_block(limit=2, max_chars=100)
        self.assertNotIn("旧记忆", block)
        self.assertIn("新记忆", block)
        self.assertIn("最新记忆", block)

        clipped = self.store.as_prompt_block(limit=2, max_chars=12)
        self.assertLessEqual(len(clipped), 12)
        self.assertTrue(clipped.endswith("…"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
