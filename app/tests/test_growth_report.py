from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "growth_report.py"


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")


class GrowthReportTest(unittest.TestCase):
    """scripts/growth_report.py：数据聚合、容错与输出契约。"""

    @staticmethod
    def _write(path: Path, value) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def _run(self, root: Path, *args: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(root), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    def _build_full_fixture(self, root: Path) -> None:
        """造齐各类数据：期内/期外混合，验证统计口径。"""
        self._write(
            root / "local" / "self" / "state.json",
            {
                "continuity_id": "self-test1234",
                "created_at": _iso(40),
                "last_reflection_at": _iso(1),
                "operational_identity": {"principles": ["证据优先于自我宣称"]},
            },
        )
        self._write(
            root / "local" / "self" / "reflections.json",
            [
                {"id": "reflection-old", "at": _iso(30), "trigger": "manual", "lessons": ["旧教训"]},
                {
                    "id": "reflection-new",
                    "at": _iso(1),
                    "trigger": "growth_daemon",
                    "lessons": ["教训一", "教训二"],
                },
            ],
        )
        self._write(
            root / "local" / "self" / "intentions.json",
            [
                {
                    "id": "intent-p0",
                    "title": "修复验证退化",
                    "priority": "P0",
                    "status": "proposed",
                    "updated_at": _iso(1),
                },
                {
                    "id": "intent-done",
                    "title": "已完成意向",
                    "priority": "P1",
                    "status": "completed",
                    "updated_at": _iso(2),
                },
                {
                    "id": "intent-blocked",
                    "title": "被阻塞意向",
                    "priority": "P2",
                    "status": "blocked",
                    "updated_at": _iso(1),
                },
                {
                    "id": "intent-old",
                    "title": "期外完成",
                    "priority": "P1",
                    "status": "completed",
                    "updated_at": _iso(30),
                },
            ],
        )
        evo_id = "evo-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-abc123"
        evo_dir = root / "data" / "promotion-history" / evo_id
        evo_dir.mkdir(parents=True)
        (evo_dir / "candidate.sha256").write_text(
            "abcdef1234567890fedcba\n", encoding="utf-8"
        )
        (evo_dir / "promoted_at").write_text(_iso(1) + "\n", encoding="utf-8")
        self._write(
            root / "local" / "self" / "optimizations.json",
            {
                "active": {"llm.temperature": {"value": 0.4, "at": _iso(1)}},
                "history": [
                    {
                        "action": "apply",
                        "key": "llm.temperature",
                        "value": 0.4,
                        "reason": "收缩采样",
                        "at": _iso(1),
                    },
                    {
                        "action": "rollback",
                        "key": "llm.temperature",
                        "value": 0.6,
                        "reason": "负反馈自动回滚：成功率下降",
                        "at": _iso(1),
                    },
                    {
                        "action": "apply",
                        "key": "llm.temperature",
                        "value": 0.2,
                        "reason": "期外动作",
                        "at": _iso(30),
                    },
                ],
            },
        )
        # capability_health 证据：一条验证失败 -> software.validation scorecard
        self._write(
            root / "data" / "validation-requests" / "val-0000000000000001.json",
            {"id": "val-1", "operation": "run_check", "target": "api", "created_at": _iso(1)},
        )
        self._write(
            root / "data" / "validation-results" / "val-0000000000000001.json",
            {"id": "val-1", "status": "failed", "summary": "boom", "finished_at": _iso(1)},
        )
        audit = root / "logs" / "audit.log"
        audit.parent.mkdir(parents=True, exist_ok=True)
        audit.write_text(
            f"[{_iso(1)}] [auth_request] auth-1 skill=x action=y\n"
            f"[{_iso(1)}] [skill_forge] name=demo origin=app-space\n"
            f"[{_iso(1)}] [optimization_apply] llm.temperature 0.6 -> 0.4 理由=收缩\n"
            f"[{_iso(30)}] [skill_forge] name=old origin=app-space\n",
            encoding="utf-8",
        )
        growth = root / "logs" / "growth.log"
        growth.write_text(
            json.dumps({"ts": _iso(1), "action": "round_start", "ok": True, "summary": "m"})
            + "\n"
            + json.dumps({"ts": _iso(1), "action": "round_done", "ok": True, "summary": "m"})
            + "\n"
            + json.dumps({"ts": _iso(30), "action": "round_done", "ok": True, "summary": "m"})
            + "\n",
            encoding="utf-8",
        )

    def test_full_fixture_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_full_fixture(root)
            out = root / "reports"
            # 让子进程可 import core.capability_health（app 包在仓库内）
            result = self._run(
                root, "--days", "7", "--out", str(out),
                extra_env={"PYTHONPATH": str(REPO_ROOT / "app")},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("报告已生成：", result.stdout)
            self.assertIn("摘要：", result.stdout)
            reports = list(out.glob("*.md"))
            self.assertEqual(len(reports), 1)
            text = reports[0].read_text(encoding="utf-8")

            # 各小节标题齐全
            for header in (
                "# Agenelf 成长报告",
                "## 自我连续性",
                "## 反思沉淀",
                "## 改进意向",
                "## 晋升历史",
                "## 参数优化",
                "## 能力健康",
                "## 运行日志事件",
                "## 下一步建议",
            ):
                self.assertIn(header, text)

            # 期内统计口径：期外数据不计入
            self.assertIn("历史反思总数：2", text)
            self.assertIn("本周期内新增：1", text)
            self.assertIn("教训一", text)
            self.assertIn("reflection-new", text)
            self.assertIn("本周期内 completed/blocked：2 条", text)
            self.assertIn("本周期内晋升：1", text)
            self.assertIn("abcdef123456", text)  # 候选摘要前 12 位
            self.assertIn("本周期内优化动作：2 次", text)
            self.assertIn("负反馈自动回滚", text)
            self.assertIn("llm.temperature", text)
            self.assertIn("可信证据总数：1", text)
            self.assertIn("software.validation", text)
            # 日志计数：授权 1、锻造期内 1（期外 1 不计）、守护轮次期内 2
            self.assertIn("| 授权 | 1 |", text)
            self.assertIn("| 技能锻造 | 1 |", text)
            self.assertIn("| 守护轮次 | 2 |", text)
            # 下一步建议取 P0/P1 开放意向（intent-done 已完成不计）
            self.assertIn("修复验证退化", text)
            self.assertIn("intent-p0", text)

    def test_empty_data_graceful(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "reports"
            result = self._run(root, "--days", "7", "--out", str(out))
            self.assertEqual(result.returncode, 0, result.stderr)
            reports = list(out.glob("*.md"))
            self.assertEqual(len(reports), 1)
            text = reports[0].read_text(encoding="utf-8")
            # 空数据：优雅标注而不是崩溃
            self.assertIn("无数据", text)
            self.assertIn("state.json 缺失或损坏", text)
            self.assertIn("保持当前节奏", text)
            self.assertIn("摘要：", result.stdout)

    def test_same_day_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "reports"
            first = self._run(root, "--out", str(out))
            second = self._run(root, "--out", str(out))
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(len(list(out.glob("*.md"))), 1)


if __name__ == "__main__":
    unittest.main()
