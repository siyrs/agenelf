"""Evidence-driven bounded self-optimization for runtime-tunable parameters.

This module is the "fast lane" of Agenelf's growth model: instead of changing
code through the app-tmp → tests → gate → promotion pipeline, it adjusts a
small whitelist of runtime parameters whose values are safe to change live.
Every adjustment is validated against hard bounds, throttled by a per-key
cooldown, recorded in bounded history, mirrored into ``logs/audit.log`` and
can be rolled back.  All state lives under ``local/self/optimizations.json``
and can be verified from files; nothing here claims subjective awareness.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .privacy import redact_sensitive_text, sanitize_value

_SCHEMA_VERSION = 1

# 可调参数白名单：只有这里的键允许被自我优化快车道修改。
TUNABLE_PARAMETERS: dict[str, dict[str, Any]] = {
    "agent.memory_prompt_limit": {
        "kind": "int",
        "default": 50,
        "min": 10,
        "max": 100,
        "description": "注入系统提示的长期记忆条数上限",
    },
    "agent.memory_prompt_max_chars": {
        "kind": "int",
        "default": 8000,
        "min": 2000,
        "max": 20000,
        "description": "注入系统提示的记忆块字符数上限",
    },
    "llm.temperature": {
        "kind": "float",
        "default": 0.6,
        "min": 0.0,
        "max": 1.0,
        "description": "每轮对话前设置到 LLM 客户端的采样温度",
    },
}

# 自动优化的固定步进比例：每次最多向一个方向调整 20%。
_AUTO_STEP_RATIO = 0.2
# 负反馈阈值：应用后成功率下降达到该比例（20 个百分点）即触发自动回滚。
_NEGATIVE_FEEDBACK_RATE_DROP = 0.2
# 负反馈阈值：应用后连续失败数增加达到该值即触发自动回滚。
_NEGATIVE_FEEDBACK_FAILURE_INCREASE = 2
# 触发记忆参数收缩所需的最少“记忆/截断”相关失败证据数。
_AUTO_MEMORY_FAILURE_THRESHOLD = 2
_MEMORY_FAILURE_KEYWORDS = (
    "memory",
    "记忆",
    "截断",
    "truncat",
    "prompt",
    "context",
    "上下文",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=".tmp", dir=path.parent, text=True
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read_json(path: Path, default: Any) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return value


def _safe_text(value: object, limit: int = 2000) -> str:
    text = redact_sensitive_text(value).strip()
    if len(text) > limit:
        return text[: max(0, limit - 1)] + "…"
    return text


def _safe_strings(value: object, *, limit: int = 10, item_limit: int = 1000) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[: max(0, limit)]:
        text = _safe_text(item, item_limit)
        if text:
            result.append(text)
    return result


def _parse_iso(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class SelfOptimizationStore:
    """Atomic, bounded store for whitelisted runtime parameter overrides."""

    def __init__(
        self,
        self_dir: str | Path,
        *,
        root: str | Path | None = None,
        max_history: int = 100,
        cooldown_seconds: int = 3600,
    ):
        self.self_dir = Path(self_dir).resolve()
        self.path = self.self_dir / "optimizations.json"
        configured_root = root or os.environ.get("AGENELF_ROOT")
        self.root = (
            Path(configured_root).resolve()
            if configured_root
            else Path(__file__).resolve().parents[2]
        )
        self.max_history = max(1, int(max_history))
        self.cooldown_seconds = max(0, int(cooldown_seconds))
        self.self_dir.mkdir(parents=True, exist_ok=True)
        self.active: dict[str, dict[str, Any]] = {}
        self.history: list[dict[str, Any]] = []
        self.cooldowns: dict[str, str] = {}
        self._load()

    # ------------------------------------------------------------------
    # 持久化与装载
    # ------------------------------------------------------------------
    def _load(self) -> None:
        value = _read_json(self.path, {})
        if not isinstance(value, dict):
            return
        sanitized = sanitize_value(value)
        if not isinstance(sanitized, dict):
            return
        active = sanitized.get("active", {})
        if isinstance(active, dict):
            for key, item in active.items():
                if key not in TUNABLE_PARAMETERS or not isinstance(item, dict):
                    continue
                ok, normalized, _ = self._validate_value(key, item.get("value"))
                if not ok:
                    continue
                entry = {
                    "value": normalized,
                    "reason": _safe_text(item.get("reason", ""), 1000),
                    "applied_at": _safe_text(item.get("applied_at", ""), 100),
                    "evidence": _safe_strings(
                        item.get("evidence", []), limit=10, item_limit=1000
                    ),
                }
                # 应用时的健康快照基线随 active 一并持久化（负反馈检查依赖）
                health_at_apply = item.get("health_at_apply")
                if isinstance(health_at_apply, dict):
                    entry["health_at_apply"] = dict(health_at_apply)
                self.active[key] = entry
        history = sanitized.get("history", [])
        if isinstance(history, list):
            self.history = [
                item for item in history if isinstance(item, dict)
            ][-self.max_history :]
        cooldowns = sanitized.get("cooldowns", {})
        if isinstance(cooldowns, dict):
            self.cooldowns = {
                str(key): str(at)
                for key, at in cooldowns.items()
                if key in TUNABLE_PARAMETERS and _parse_iso(at) is not None
            }

    def _save(self) -> None:
        self.history = self.history[-self.max_history :]
        _atomic_json(
            self.path,
            {
                "schema_version": _SCHEMA_VERSION,
                "updated_at": _now_iso(),
                "active": self.active,
                "history": self.history,
                "cooldowns": self.cooldowns,
                "consciousness_claim": False,
            },
        )

    def _audit(self, event: str, detail: str) -> None:
        """Best-effort 审计：审计失败绝不影响对话与优化主流程。"""

        path = self.root / "logs" / "audit.log"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"[{_now_iso()}] [{event}] {detail}\n")
        except OSError:
            pass

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_value(key: str, value: object) -> tuple[bool, Any, str]:
        spec = TUNABLE_PARAMETERS[key]
        kind = spec["kind"]
        if kind == "int":
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                return False, None, f"值 {value!r} 无法解析为整数"
            try:
                as_float = float(str(value))
                if not as_float.is_integer():
                    return False, None, f"值 {value!r} 必须是整数"
                normalized: Any = int(as_float)
            except (TypeError, ValueError):
                return False, None, f"值 {value!r} 无法解析为整数"
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                return False, None, f"值 {value!r} 无法解析为浮点数"
            try:
                normalized = float(str(value))
            except (TypeError, ValueError):
                return False, None, f"值 {value!r} 无法解析为浮点数"
        lower = spec["min"]
        upper = spec["max"]
        if normalized < lower or normalized > upper:
            return (
                False,
                None,
                f"值 {normalized} 越界：{key} 允许范围是 [{lower}, {upper}]",
            )
        return True, normalized, ""

    def _check_key(self, key: object) -> tuple[bool, str]:
        normalized = str(key or "").strip()
        if normalized not in TUNABLE_PARAMETERS:
            allowed = ", ".join(sorted(TUNABLE_PARAMETERS))
            return False, f"拒绝：{normalized or key!r} 不在可调参数白名单内（{allowed}）"
        return True, normalized

    def _cooldown_remaining(self, key: str) -> float:
        last = _parse_iso(self.cooldowns.get(key))
        if last is None:
            return 0.0
        elapsed = (_now() - last).total_seconds()
        return max(0.0, self.cooldown_seconds - elapsed)

    def _validate_change(self, key: object, value: object) -> tuple[bool, str, Any, str]:
        normalized_key = str(key or "").strip()
        if normalized_key not in TUNABLE_PARAMETERS:
            allowed = ", ".join(sorted(TUNABLE_PARAMETERS))
            return (
                False,
                normalized_key,
                None,
                f"拒绝：{normalized_key or key!r} 不在可调参数白名单内（{allowed}）",
            )
        ok, normalized_value, message = self._validate_value(normalized_key, value)
        if not ok:
            return False, normalized_key, None, message
        remaining = self._cooldown_remaining(normalized_key)
        if remaining > 0:
            return (
                False,
                normalized_key,
                None,
                f"拒绝：{normalized_key} 处于冷却期，还需等待 {int(remaining)} 秒",
            )
        return True, normalized_key, normalized_value, ""

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------
    def get_effective(self, key: str, default: Any) -> Any:
        """运行时查询：active 中存在已验证覆盖则返回覆盖值，否则返回默认值。"""

        item = self.active.get(str(key or "").strip())
        if isinstance(item, dict) and "value" in item:
            return item["value"]
        return default

    def propose(
        self,
        key: str,
        value: object,
        reason: str,
        evidence: list[str] | None = None,
    ) -> tuple[bool, str]:
        """只校验不写入：白名单、范围与冷却期全部通过才返回 True。"""

        ok, normalized_key, normalized_value, message = self._validate_change(key, value)
        if not ok:
            return False, message
        current = self.get_effective(
            normalized_key, TUNABLE_PARAMETERS[normalized_key]["default"]
        )
        _ = _safe_text(reason, 1000)  # 理由在 apply 时才持久化，这里仅保持接口一致
        return (
            True,
            f"校验通过：{normalized_key} 可从 {current} 调整为 {normalized_value}",
        )

    def apply(
        self,
        key: str,
        value: object,
        reason: str,
        evidence: list[str] | None = None,
    ) -> tuple[bool, str]:
        """校验通过后写入 active 与有界 history，并追加审计日志。"""

        ok, normalized_key, normalized_value, message = self._validate_change(key, value)
        if not ok:
            return False, message
        spec = TUNABLE_PARAMETERS[normalized_key]
        previous_item = self.active.get(normalized_key)
        previous = previous_item.get("value") if isinstance(previous_item, dict) else None
        safe_reason = _safe_text(reason, 1000) or "未提供理由"
        safe_evidence = _safe_strings(evidence or [], limit=10, item_limit=1000)
        at = _now_iso()
        self.active[normalized_key] = {
            "value": normalized_value,
            "reason": safe_reason,
            "applied_at": at,
            "evidence": safe_evidence,
            # 应用瞬间的能力健康基线：供 auto_tune 的负反馈检查对比。
            # 模块不可用或读取失败时容错记 None。
            "health_at_apply": self._health_summary(),
        }
        self.history.append(
            {
                "action": "apply",
                "key": normalized_key,
                "value": normalized_value,
                "previous": previous,
                "reason": safe_reason,
                "evidence": safe_evidence,
                "at": at,
            }
        )
        self.cooldowns[normalized_key] = at
        self._save()
        self._audit(
            "optimization_apply",
            f"{normalized_key} {previous if previous is not None else spec['default']} -> {normalized_value} 理由={safe_reason}",
        )
        return (
            True,
            f"已应用：{normalized_key} = {normalized_value}（前值 "
            f"{previous if previous is not None else spec['default']}）",
        )

    def rollback(self, key: str) -> tuple[bool, str]:
        """回滚到上一个历史值；没有更早的历史值时删除 active 项。"""

        ok, normalized_key = self._check_key(key)
        if not ok:
            return False, normalized_key
        last_apply = next(
            (
                item
                for item in reversed(self.history)
                if item.get("key") == normalized_key and item.get("action") == "apply"
            ),
            None,
        )
        if normalized_key not in self.active and last_apply is None:
            return False, f"没有可回滚的记录：{normalized_key}"
        previous = last_apply.get("previous") if last_apply else None
        at = _now_iso()
        if previous is None:
            self.active.pop(normalized_key, None)
            message = f"已回滚：{normalized_key} 无更早历史值，已恢复默认"
        else:
            current_item = self.active.get(normalized_key, {})
            self.active[normalized_key] = {
                "value": previous,
                "reason": "回滚到上一个历史值",
                "applied_at": at,
                "evidence": _safe_strings(
                    current_item.get("evidence", []) if isinstance(current_item, dict) else [],
                    limit=10,
                    item_limit=1000,
                ),
            }
            message = f"已回滚：{normalized_key} 恢复为 {previous}"
        self.history.append(
            {
                "action": "rollback",
                "key": normalized_key,
                "value": previous,
                "previous": None,
                "reason": "手动或自动回滚",
                "evidence": [],
                "at": at,
            }
        )
        self.cooldowns[normalized_key] = at
        self._save()
        self._audit("optimization_rollback", f"{normalized_key} -> {previous!r}")
        return True, message

    def status(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "path": str(self.path),
            "active": {key: dict(item) for key, item in self.active.items()},
            "history_count": len(self.history),
            "recent_history": [dict(item) for item in reversed(self.history[-10:])],
            "cooldowns": dict(self.cooldowns),
            "whitelist": {
                key: {
                    "kind": spec["kind"],
                    "default": spec["default"],
                    "min": spec["min"],
                    "max": spec["max"],
                    "description": spec["description"],
                }
                for key, spec in sorted(TUNABLE_PARAMETERS.items())
            },
            "policy": {
                "max_history": self.max_history,
                "cooldown_seconds": self.cooldown_seconds,
                "auto_step_ratio": _AUTO_STEP_RATIO,
                "config_yaml_mutable": False,
                "consciousness_claim": False,
            },
        }

    # ------------------------------------------------------------------
    # 证据驱动自动优化（不调用 LLM）
    # ------------------------------------------------------------------
    @staticmethod
    def _summary_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
        """把 capability_health 快照聚合为可对比的关键指标。"""

        scorecards = snapshot.get("scorecards", {}) if isinstance(snapshot, dict) else {}
        observations = 0
        terminal = 0
        succeeded = 0
        consecutive_failures = 0
        if isinstance(scorecards, dict):
            for card in scorecards.values():
                if not isinstance(card, dict):
                    continue
                observations += int(card.get("observations", 0) or 0)
                terminal += int(card.get("terminal_observations", 0) or 0)
                succeeded += int(card.get("succeeded", 0) or 0)
                consecutive_failures = max(
                    consecutive_failures, int(card.get("consecutive_failures", 0) or 0)
                )
        return {
            "observed_at": (
                str(snapshot.get("observed_at", "")) if isinstance(snapshot, dict) else ""
            ),
            "observations": observations,
            "terminal_observations": terminal,
            "success_rate": round(succeeded / terminal, 4) if terminal else None,
            "consecutive_failures": consecutive_failures,
        }

    def _health_summary(self) -> dict[str, Any] | None:
        """Best-effort 健康摘要：模块不可用或读取失败时容错返回 None。"""

        try:
            from .capability_health import CapabilityHealth
        except ImportError:
            return None
        try:
            snapshot = CapabilityHealth(self.root).snapshot()
        except Exception:
            return None
        return self._summary_from_snapshot(snapshot)

    def _negative_feedback_check(
        self, current: dict[str, Any], result: dict[str, Any]
    ) -> None:
        """对持有健康基线的 active 键做负反馈检查，恶化即自动回滚。

        判定（任一满足）：成功率较应用时下降 ≥20 个百分点，或连续失败数
        较应用时增加 ≥2。回滚走现有 rollback 审计链，并追加一条注明
        “负反馈自动回滚”的审计记录。
        """

        if not isinstance(current, dict):
            return
        for key in sorted(list(self.active)):
            item = self.active.get(key)
            if not isinstance(item, dict):
                continue
            baseline = item.get("health_at_apply")
            if not isinstance(baseline, dict):
                continue
            reasons: list[str] = []
            base_rate = baseline.get("success_rate")
            curr_rate = current.get("success_rate")
            if isinstance(base_rate, (int, float)) and isinstance(
                curr_rate, (int, float)
            ):
                drop = float(base_rate) - float(curr_rate)
                if drop >= _NEGATIVE_FEEDBACK_RATE_DROP:
                    reasons.append(
                        f"成功率下降 {drop:.0%}（{base_rate} -> {curr_rate}）"
                    )
            base_failures = int(baseline.get("consecutive_failures", 0) or 0)
            curr_failures = int(current.get("consecutive_failures", 0) or 0)
            if curr_failures - base_failures >= _NEGATIVE_FEEDBACK_FAILURE_INCREASE:
                reasons.append(
                    "连续失败数增加 "
                    f"{curr_failures - base_failures}（{base_failures} -> {curr_failures}）"
                )
            if not reasons:
                continue
            detail = "；".join(reasons)
            rolled_back, message = self.rollback(key)
            self._audit(
                "optimization_auto_rollback",
                f"负反馈自动回滚 {key}：{detail}；{message}",
            )
            result["auto_rollbacks"].append(
                {
                    "key": key,
                    "rolled_back": rolled_back,
                    "reason": f"负反馈自动回滚：{detail}",
                    "message": message,
                    "health_at_apply": baseline,
                    "health_now": current,
                }
            )

    def _step_apply(
        self,
        key: str,
        target: Any,
        *,
        reason: str,
        evidence: list[str],
        actions: list[dict[str, Any]],
    ) -> None:
        current = self.get_effective(key, TUNABLE_PARAMETERS[key]["default"])
        if target == current:
            return
        ok, message = self.apply(key, target, reason, evidence)
        actions.append(
            {
                "key": key,
                "from": current,
                "to": target,
                "applied": ok,
                "message": message,
            }
        )

    def auto_tune(self) -> dict[str, Any]:
        """基于 capability_health 可信结果的确定性自动优化。

        不调用 LLM；无证据时明确保持现状；所有动作复用 apply 的
        白名单、范围、冷却与审计链。第一步固定为负反馈检查：凡应用后
        健康恶化的键先自动回滚，再进入证据驱动的步进调整。
        """

        result: dict[str, Any] = {
            "observed_at": _now_iso(),
            "evidence": [],
            "actions": [],
            "auto_rollbacks": [],
            "note": "",
            "consciousness_claim": False,
        }
        try:
            from .capability_health import CapabilityHealth
        except ImportError:
            result["note"] = "能力健康模块不可用：证据不足，保持现状"
            return result
        try:
            snapshot = CapabilityHealth(self.root).snapshot()
        except Exception:
            result["note"] = "能力健康快照不可用：证据不足，保持现状"
            return result
        # 第一步：负反馈检查 —— 优化后健康恶化的键自动回滚（见 auto_rollbacks）。
        self._negative_feedback_check(self._summary_from_snapshot(snapshot), result)
        evidence_items = [
            item
            for item in snapshot.get("recent_evidence", [])
            if isinstance(item, dict)
        ]
        result["evidence"] = [
            f"{item.get('source_id', 'unknown')}:{item.get('status', 'unknown')}"
            for item in evidence_items[-10:]
        ]
        if not evidence_items:
            result["note"] = "证据不足，保持现状"
            self._prepend_rollback_note(result)
            return result

        failures = [item for item in evidence_items if item.get("success") is False]
        memory_failures = []
        for item in failures:
            haystack = " ".join(
                str(item.get(field, ""))
                for field in ("summary", "target", "operation", "capability")
            ).lower()
            if any(keyword in haystack for keyword in _MEMORY_FAILURE_KEYWORDS):
                memory_failures.append(item)

        key = "agent.memory_prompt_max_chars"
        spec = TUNABLE_PARAMETERS[key]
        current = int(self.get_effective(key, spec["default"]))
        if len(memory_failures) >= _AUTO_MEMORY_FAILURE_THRESHOLD:
            # 记忆相关失败/截断证据足够多：缩小一档（20% 步进）。
            target = max(spec["min"], int(round(current * (1 - _AUTO_STEP_RATIO))))
            refs = [
                str(item.get("evidence_path") or item.get("source_id") or "")
                for item in memory_failures[-5:]
            ]
            self._step_apply(
                key,
                target,
                reason=(
                    f"自动优化：检测到 {len(memory_failures)} 条记忆/截断相关失败证据，"
                    "缩小记忆提示块一档以降低截断风险"
                ),
                evidence=refs,
                actions=result["actions"],
            )
            if result["actions"]:
                result["note"] = "根据记忆相关失败证据执行了一档收缩"
            else:
                result["note"] = "已处于允许下限，保持现状"
        elif not failures:
            # 连续健康：向默认值方向回调一档（20% 步进，不越过默认值）。
            default = int(spec["default"])
            if current < default:
                target = min(default, int(round(current * (1 + _AUTO_STEP_RATIO))))
                self._step_apply(
                    key,
                    target,
                    reason="自动优化：可信结果连续健康，向默认值回调一档",
                    evidence=[
                        f"capability_health:{snapshot.get('observed_at', '')}"
                    ],
                    actions=result["actions"],
                )
                result["note"] = "证据连续健康，向默认值回调一档"
            else:
                result["note"] = "证据连续健康且参数已处于默认值，保持现状"
        else:
            result["note"] = (
                f"存在 {len(failures)} 条失败证据，但与记忆/截断无关，保持现状"
            )
        self._prepend_rollback_note(result)
        return result

    @staticmethod
    def _prepend_rollback_note(result: dict[str, Any]) -> None:
        """发生负反馈自动回滚时，在 note 前固定标注，便于审计阅读。"""

        auto_rollbacks = result.get("auto_rollbacks") or []
        if auto_rollbacks:
            keys = ", ".join(str(item.get("key", "?")) for item in auto_rollbacks)
            prefix = f"负反馈自动回滚 {len(auto_rollbacks)} 个键（{keys}）"
            note = str(result.get("note", ""))
            result["note"] = f"{prefix}；{note}" if note else prefix
