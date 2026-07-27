"""晋升请求目录合并读取的单元测试。

gate_check.sh 默认把候选晋升请求写入 app-tmp/promote-requests（可用
PROMOTE_REQUESTS_DIR 覆盖），promote.sh 校验通过后才由宿主机移入
data/promote-requests。读取方（skills.evolution_ops / api /evolution/status）
需合并两个目录并标注来源。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from skills import evolution_ops  # noqa: E402


def _make_request(directory: Path, request_id: str, *markers: str, mtime: float) -> Path:
    entry = directory / request_id
    entry.mkdir(parents=True, exist_ok=True)
    for marker in markers:
        (entry / marker).write_text("ok\n", encoding="utf-8")
    os.utime(entry, (mtime, mtime))
    return entry


class MergedPromotionRequestsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.candidate = self.root / "app-tmp" / "promote-requests"
        self.promoted = self.root / "data" / "promote-requests"
        self.old_env = {
            key: os.environ.get(key)
            for key in ("AGENELF_ROOT", "PROMOTE_REQUESTS_DIR")
        }
        os.environ["AGENELF_ROOT"] = str(self.root)
        os.environ.pop("PROMOTE_REQUESTS_DIR", None)

    def tearDown(self):
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_missing_directories_are_tolerated(self):
        self.assertEqual(evolution_ops.merged_promotion_requests(self.root), [])

    def test_candidate_only_is_labeled_candidate(self):
        _make_request(self.candidate, "req-aaa", "READY", mtime=1000)
        rows = evolution_ops.merged_promotion_requests(self.root)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "req-aaa")
        self.assertEqual(rows[0]["source"], "candidate")
        self.assertEqual(rows[0]["markers"], ["READY"])

    def test_promoted_only_is_labeled_promoted(self):
        _make_request(self.promoted, "req-bbb", "READY", "PROMOTED", mtime=1000)
        rows = evolution_ops.merged_promotion_requests(self.root)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "promoted")
        self.assertEqual(rows[0]["markers"], ["PROMOTED", "READY"])

    def test_duplicate_id_promoted_wins(self):
        _make_request(self.candidate, "req-dup", "READY", mtime=2000)
        _make_request(self.promoted, "req-dup", "READY", "PROMOTED", mtime=1000)
        rows = evolution_ops.merged_promotion_requests(self.root)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "promoted")
        self.assertEqual(rows[0]["markers"], ["PROMOTED", "READY"])

    def test_both_directories_merged_and_sorted_by_mtime_desc(self):
        _make_request(self.candidate, "req-new", "READY", mtime=3000)
        _make_request(self.promoted, "req-old", "PROMOTED", mtime=1000)
        _make_request(self.promoted, "req-mid", "READY", mtime=2000)
        rows = evolution_ops.merged_promotion_requests(self.root)
        self.assertEqual([row["id"] for row in rows], ["req-new", "req-mid", "req-old"])
        self.assertEqual(
            [row["source"] for row in rows],
            ["candidate", "promoted", "promoted"],
        )

    def test_limit_is_applied(self):
        for index in range(5):
            _make_request(self.candidate, f"req-{index}", "READY", mtime=1000 + index)
        rows = evolution_ops.merged_promotion_requests(self.root, limit=2)
        self.assertEqual([row["id"] for row in rows], ["req-4", "req-3"])

    def test_promote_requests_dir_env_override(self):
        custom = self.root / "custom-gate-output"
        _make_request(custom, "req-env", "READY", mtime=1000)
        os.environ["PROMOTE_REQUESTS_DIR"] = str(custom)
        rows = evolution_ops.merged_promotion_requests(self.root)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "req-env")
        self.assertEqual(rows[0]["source"], "candidate")

    def test_evolution_status_text_marks_source(self):
        _make_request(self.candidate, "req-cand", "READY", mtime=2000)
        _make_request(self.promoted, "req-done", "PROMOTED", mtime=1000)
        text = evolution_ops.evolution_status()
        self.assertIn("req-cand（candidate）", text)
        self.assertIn("req-done（promoted）", text)

    def test_evolution_status_without_requests(self):
        text = evolution_ops.evolution_status()
        self.assertIn("暂无晋升请求记录", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
