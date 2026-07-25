from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import capability_health
from core.self_optimization import SelfOptimizationStore


class SelfOptimizationStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "local" / "self").mkdir(parents=True)
        (self.root / "logs").mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _store(self, **kwargs) -> SelfOptimizationStore:
        return SelfOptimizationStore(
            self.root / "local" / "self", root=self.root, **kwargs
        )

    def _write_validation(self, index: int, *, status: str, summary: str) -> None:
        validation_id = f"val-{index:016x}"
        request = self.root / "data" / "validation-requests" / f"{validation_id}.json"
        result = self.root / "data" / "validation-results" / f"{validation_id}.json"
        request.parent.mkdir(parents=True, exist_ok=True)
        result.parent.mkdir(parents=True, exist_ok=True)
        stamp = f"2026-01-01T00:00:0{index}+00:00"
        request.write_text(
            json.dumps(
                {
                    "id": validation_id,
                    "operation": "run_check",
                    "target": "memory-prompt",
                    "created_at": stamp,
                }
            ),
            encoding="utf-8",
        )
        result.write_text(
            json.dumps(
                {
                    "id": validation_id,
                    "status": status,
                    "summary": summary,
                    "finished_at": stamp,
                }
            ),
            encoding="utf-8",
        )

    def test_default_fallback_without_active(self):
        store = self._store()
        self.assertEqual(store.get_effective("agent.memory_prompt_limit", 50), 50)
        self.assertEqual(store.get_effective("llm.temperature", 0.6), 0.6)
        self.assertEqual(store.get_effective("unknown.key", "x"), "x")

    def test_out_of_range_values_are_rejected(self):
        store = self._store()
        ok, message = store.apply("agent.memory_prompt_limit", 5, "越界测试")
        self.assertFalse(ok)
        self.assertIn("越界", message)
        ok, _ = store.apply("agent.memory_prompt_max_chars", 50000, "越界测试")
        self.assertFalse(ok)
        ok, _ = store.apply("llm.temperature", 1.5, "越界测试")
        self.assertFalse(ok)
        self.assertEqual(store.status()["active"], {})
        # propose 同样拒绝且不留痕迹
        ok, _ = store.propose("agent.memory_prompt_limit", 5, "越界测试")
        self.assertFalse(ok)

    def test_non_whitelist_key_is_rejected(self):
        store = self._store()
        ok, message = store.apply("agent.max_tool_rounds", 3, "白名单外")
        self.assertFalse(ok)
        self.assertIn("白名单", message)
        ok, _ = store.rollback("agent.max_tool_rounds")
        self.assertFalse(ok)
        self.assertFalse((self.root / "config.yaml").exists())

    def test_apply_then_rollback_restores_default(self):
        store = self._store()
        ok, _ = store.apply("agent.memory_prompt_limit", 10, "缩小记忆条数")
        self.assertTrue(ok)
        self.assertEqual(store.get_effective("agent.memory_prompt_limit", 50), 10)
        ok, message = store.rollback("agent.memory_prompt_limit")
        self.assertTrue(ok)
        self.assertIn("默认", message)
        self.assertEqual(store.get_effective("agent.memory_prompt_limit", 50), 50)

    def test_rollback_returns_to_previous_history_value(self):
        store = self._store(cooldown_seconds=0)
        store.apply("agent.memory_prompt_limit", 10, "第一步")
        store.apply("agent.memory_prompt_limit", 20, "第二步")
        ok, _ = store.rollback("agent.memory_prompt_limit")
        self.assertTrue(ok)
        self.assertEqual(store.get_effective("agent.memory_prompt_limit", 50), 10)

    def test_cooldown_rejects_same_key_within_window(self):
        store = self._store(cooldown_seconds=3600)
        ok, _ = store.apply("llm.temperature", 0.2, "第一次")
        self.assertTrue(ok)
        ok, message = store.apply("llm.temperature", 0.3, "冷却期内第二次")
        self.assertFalse(ok)
        self.assertIn("冷却期", message)
        ok, message = store.propose("llm.temperature", 0.3, "冷却期内")
        self.assertFalse(ok)
        # 其他键不受同键冷却影响
        ok, _ = store.apply("agent.memory_prompt_limit", 30, "不同键")
        self.assertTrue(ok)

    def test_auto_tune_keeps_status_quo_without_evidence(self):
        store = self._store()
        result = store.auto_tune()
        self.assertEqual(result["actions"], [])
        self.assertIn("证据不足", result["note"])
        self.assertEqual(store.status()["active"], {})

    def test_auto_tune_shrinks_memory_block_on_failure_evidence(self):
        self._write_validation(1, status="failed", summary="memory prompt 截断：检查失败")
        self._write_validation(2, status="failed", summary="上下文记忆块被截断")
        store = self._store()
        result = store.auto_tune()
        self.assertEqual(len(result["actions"]), 1)
        action = result["actions"][0]
        self.assertEqual(action["key"], "agent.memory_prompt_max_chars")
        self.assertTrue(action["applied"])
        self.assertEqual(action["from"], 8000)
        self.assertEqual(action["to"], 6400)
        self.assertEqual(store.get_effective("agent.memory_prompt_max_chars", 8000), 6400)

    def test_auto_tune_steps_back_toward_default_when_healthy(self):
        self._write_validation(1, status="succeeded", summary="全部检查通过")
        store = self._store(cooldown_seconds=0)
        store.apply("agent.memory_prompt_max_chars", 5000, "手动收缩")
        result = store.auto_tune()
        self.assertEqual(len(result["actions"]), 1)
        action = result["actions"][0]
        self.assertTrue(action["applied"])
        self.assertEqual(action["to"], 6000)
        self.assertEqual(store.get_effective("agent.memory_prompt_max_chars", 8000), 6000)

    def test_audit_log_records_apply_and_rollback(self):
        store = self._store(cooldown_seconds=0)
        store.apply("agent.memory_prompt_limit", 10, "审计测试")
        store.rollback("agent.memory_prompt_limit")
        log = (self.root / "logs" / "audit.log").read_text(encoding="utf-8")
        self.assertIn("optimization_apply", log)
        self.assertIn("optimization_rollback", log)
        self.assertIn("agent.memory_prompt_limit", log)

    def test_history_is_bounded(self):
        store = self._store(max_history=5, cooldown_seconds=0)
        for value in (10, 20, 30, 40, 50, 60, 70):
            ok, _ = store.apply("agent.memory_prompt_limit", value, "有界测试")
            self.assertTrue(ok)
        self.assertLessEqual(len(store.history), 5)
        # 持久化文件同样有界
        persisted = json.loads(store.path.read_text(encoding="utf-8"))
        self.assertLessEqual(len(persisted["history"]), 5)

    def test_active_value_is_persisted_and_sensitive_text_redacted(self):
        store = self._store()
        store.apply(
            "agent.memory_prompt_limit",
            10,
            "修复 token=super-secret-value 相关问题",
        )
        reloaded = self._store()
        self.assertEqual(reloaded.get_effective("agent.memory_prompt_limit", 50), 10)
        reason = reloaded.status()["active"]["agent.memory_prompt_limit"]["reason"]
        self.assertNotIn("super-secret-value", reason)

    def test_rollback_without_records_is_rejected(self):
        store = self._store()
        ok, message = store.rollback("agent.memory_prompt_limit")
        self.assertFalse(ok)
        self.assertIn("没有可回滚", message)

    # ------------------------------------------------------------------
    # 负反馈自动回滚
    # ------------------------------------------------------------------
    def _write_validation_result(
        self, index: int, *, status: str, summary: str, target: str = "disk-check"
    ) -> None:
        """写入一条目标不含记忆关键词的软件验证证据（隔离负反馈变量）。"""

        validation_id = f"val-{index:016x}"
        request = self.root / "data" / "validation-requests" / f"{validation_id}.json"
        result = self.root / "data" / "validation-results" / f"{validation_id}.json"
        request.parent.mkdir(parents=True, exist_ok=True)
        result.parent.mkdir(parents=True, exist_ok=True)
        stamp = f"2026-01-01T00:00:0{index}+00:00"
        request.write_text(
            json.dumps(
                {
                    "id": validation_id,
                    "operation": "run_check",
                    "target": target,
                    "created_at": stamp,
                }
            ),
            encoding="utf-8",
        )
        result.write_text(
            json.dumps(
                {
                    "id": validation_id,
                    "status": status,
                    "summary": summary,
                    "finished_at": stamp,
                }
            ),
            encoding="utf-8",
        )

    def test_apply_records_health_snapshot_baseline(self):
        self._write_validation_result(1, status="succeeded", summary="检查通过")
        self._write_validation_result(2, status="failed", summary="磁盘检查失败")
        store = self._store()
        ok, _ = store.apply("llm.temperature", 0.4, "记录基线")
        self.assertTrue(ok)
        baseline = store.active["llm.temperature"].get("health_at_apply")
        self.assertIsInstance(baseline, dict)
        self.assertEqual(baseline["observations"], 2)
        self.assertEqual(baseline["success_rate"], 0.5)
        self.assertEqual(baseline["consecutive_failures"], 1)
        # 基线随 active 持久化，重载后仍可用于负反馈对比
        reloaded = self._store()
        self.assertEqual(
            reloaded.active["llm.temperature"]["health_at_apply"], baseline
        )

    def test_apply_tolerates_health_snapshot_failure(self):
        store = self._store()
        with mock.patch.object(
            capability_health.CapabilityHealth,
            "snapshot",
            side_effect=RuntimeError("注入快照故障"),
        ):
            ok, _ = store.apply("llm.temperature", 0.4, "容错测试")
            self.assertTrue(ok)
            self.assertIsNone(store.active["llm.temperature"]["health_at_apply"])
            result = store.auto_tune()
        self.assertEqual(result["auto_rollbacks"], [])
        self.assertIn("不可用", result["note"])

    def test_auto_tune_auto_rolls_back_when_health_degrades(self):
        # 应用时健康：3 次成功观测，成功率 1.0、连续失败 0
        for index in (1, 2, 3):
            self._write_validation_result(index, status="succeeded", summary="检查通过")
        store = self._store(cooldown_seconds=0)
        ok, _ = store.apply("llm.temperature", 0.4, "尝试降温")
        self.assertTrue(ok)
        # 优化后健康恶化：新增 2 次连续失败 → 成功率 1.0 -> 0.6，连续失败 0 -> 2
        self._write_validation_result(4, status="failed", summary="磁盘检查失败")
        self._write_validation_result(5, status="failed", summary="磁盘检查再次失败")
        result = store.auto_tune()
        self.assertEqual(len(result["auto_rollbacks"]), 1)
        rollback = result["auto_rollbacks"][0]
        self.assertEqual(rollback["key"], "llm.temperature")
        self.assertTrue(rollback["rolled_back"])
        self.assertIn("负反馈自动回滚", rollback["reason"])
        # 回滚走现有审计链：前值 None，恢复默认 0.6
        self.assertEqual(store.get_effective("llm.temperature", 0.6), 0.6)
        self.assertIn("负反馈自动回滚", result["note"])
        log = (self.root / "logs" / "audit.log").read_text(encoding="utf-8")
        self.assertIn("optimization_auto_rollback", log)
        self.assertIn("负反馈自动回滚", log)

    def test_auto_tune_no_rollback_when_health_stable(self):
        self._write_validation_result(1, status="succeeded", summary="检查通过")
        store = self._store(cooldown_seconds=0)
        ok, _ = store.apply("llm.temperature", 0.4, "稳定基线")
        self.assertTrue(ok)
        result = store.auto_tune()
        self.assertEqual(result["auto_rollbacks"], [])
        self.assertEqual(store.get_effective("llm.temperature", 0.6), 0.4)

    def test_auto_tune_no_rollback_when_health_improves(self):
        # 应用时已有失败基线（成功率 0.0、连续失败 2）
        self._write_validation_result(1, status="failed", summary="磁盘检查失败")
        self._write_validation_result(2, status="failed", summary="磁盘检查再次失败")
        store = self._store(cooldown_seconds=0)
        ok, _ = store.apply("llm.temperature", 0.4, "恶化期应用")
        self.assertTrue(ok)
        # 之后恢复健康：成功率上升、连续失败清零 → 不触发负反馈
        for index in (3, 4, 5):
            self._write_validation_result(index, status="succeeded", summary="检查通过")
        result = store.auto_tune()
        self.assertEqual(result["auto_rollbacks"], [])
        self.assertEqual(store.get_effective("llm.temperature", 0.6), 0.4)

    def test_no_auto_rollback_without_baseline(self):
        # 旧版本留下的 active 没有 health_at_apply：即使恶化也不回滚
        store = self._store(cooldown_seconds=0)
        ok, _ = store.apply("llm.temperature", 0.4, "无基线")
        self.assertTrue(ok)
        store.active["llm.temperature"].pop("health_at_apply")
        store._save()
        self._write_validation_result(1, status="failed", summary="磁盘检查失败")
        self._write_validation_result(2, status="failed", summary="磁盘检查再次失败")
        result = store.auto_tune()
        self.assertEqual(result["auto_rollbacks"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
