"""Evidence-derived capability health for Agenelf's operational self-model.

This module does not trust model prose.  It derives scorecards from deterministic
runner results and autonomy-cycle records already written to the runtime data
folders.  The resulting health view can feed self-assessment and reflection without
claiming subjective awareness.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _status_success(status: object) -> bool | None:
    normalized = str(status or "").lower()
    if normalized in {"succeeded", "completed", "promotion_requested"}:
        return True
    if normalized in {"failed", "blocked", "rejected", "denied"}:
        return False
    return None


def _safe_summary(value: object, limit: int = 500) -> str:
    text = str(value or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


class CapabilityHealth:
    """Build rolling scorecards from trusted runtime evidence."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def _operation_evidence(self) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        directory = self.root / "data" / "ops-results"
        if not directory.is_dir():
            return values
        requests = self.root / "data" / "ops-requests"
        for path in sorted(directory.glob("op-*.json")):
            result = _read_json(path)
            if result is None:
                continue
            request = _read_json(requests / path.name) or {}
            status = str(result.get("status", "unknown"))
            values.append(
                {
                    "source_type": "operation",
                    "source_id": path.stem,
                    "capability": str(request.get("capability") or "server.operations"),
                    "operation": str(request.get("operation") or result.get("operation") or "unknown"),
                    "target": str(request.get("target") or result.get("target") or "unknown"),
                    "status": status,
                    "success": _status_success(status),
                    "observed_at": str(result.get("finished_at") or request.get("created_at") or ""),
                    "summary": _safe_summary(result.get("reason") or request.get("summary") or status),
                    "evidence_path": str(path.relative_to(self.root)),
                }
            )
        return values

    def _validation_evidence(self) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        directory = self.root / "data" / "validation-results"
        if not directory.is_dir():
            return values
        requests = self.root / "data" / "validation-requests"
        for path in sorted(directory.glob("val-*.json")):
            result = _read_json(path)
            if result is None:
                continue
            request = _read_json(requests / path.name) or {}
            status = str(result.get("status", "unknown"))
            values.append(
                {
                    "source_type": "validation",
                    "source_id": path.stem,
                    "capability": "software.validation",
                    "operation": str(request.get("operation") or result.get("operation") or "unknown"),
                    "target": str(request.get("target") or result.get("target") or "unknown"),
                    "status": status,
                    "success": _status_success(status),
                    "observed_at": str(result.get("finished_at") or request.get("created_at") or ""),
                    "summary": _safe_summary(result.get("summary") or result.get("reason") or status),
                    "evidence_path": str(path.relative_to(self.root)),
                }
            )
        return values

    def _repair_evidence(self) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        directory = self.root / "data" / "repair-results"
        if not directory.is_dir():
            return values
        requests = self.root / "data" / "repair-requests"
        for path in sorted(directory.glob("repair-*.json")):
            result = _read_json(path)
            if result is None:
                continue
            request = _read_json(requests / path.name) or {}
            status = str(result.get("status", "unknown"))
            values.append(
                {
                    "source_type": "code_repair",
                    "source_id": path.stem,
                    "capability": "code.repair",
                    "operation": "apply_patch_and_test",
                    "target": str(result.get("repository") or request.get("target") or "unknown"),
                    "status": status,
                    "success": _status_success(status),
                    "observed_at": str(result.get("finished_at") or request.get("created_at") or ""),
                    "summary": _safe_summary(result.get("summary") or result.get("reason") or status),
                    "evidence_path": str(path.relative_to(self.root)),
                }
            )
        return values

    def _autonomy_evidence(self) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        directory = self.root / "data" / "autonomy-cycles"
        if not directory.is_dir():
            return values
        for path in sorted(directory.glob("auto-*.json")):
            cycle = _read_json(path)
            if cycle is None:
                continue
            status = str(cycle.get("status", "unknown"))
            success = _status_success(status)
            if success is None and status not in {"plan_ready", "planned"}:
                continue
            values.append(
                {
                    "source_type": "autonomy",
                    "source_id": path.stem,
                    "capability": "agent.self_development",
                    "operation": "autonomy_cycle",
                    "target": str(cycle.get("source_intention_id") or "self"),
                    "status": status,
                    "success": success,
                    "observed_at": str(cycle.get("updated_at") or cycle.get("started_at") or ""),
                    "summary": _safe_summary(cycle.get("error") or cycle.get("goal") or status),
                    "evidence_path": str(path.relative_to(self.root)),
                }
            )
        return values

    def evidence(self, limit: int = 500) -> list[dict[str, Any]]:
        values = (
            self._operation_evidence()
            + self._validation_evidence()
            + self._repair_evidence()
            + self._autonomy_evidence()
        )
        values.sort(key=lambda item: (str(item.get("observed_at", "")), str(item.get("source_id", ""))))
        return values[-max(0, int(limit)) :]

    @staticmethod
    def _scorecard(capability: str, values: list[dict[str, Any]]) -> dict[str, Any]:
        terminal = [item for item in values if item.get("success") in {True, False}]
        succeeded = sum(1 for item in terminal if item.get("success") is True)
        failed = sum(1 for item in terminal if item.get("success") is False)
        consecutive_failures = 0
        for item in reversed(terminal):
            if item.get("success") is False:
                consecutive_failures += 1
            else:
                break
        total = len(terminal)
        success_rate = round(succeeded / total, 4) if total else None
        if total == 0:
            health = "unknown"
        elif consecutive_failures >= 2 or (total >= 3 and success_rate is not None and success_rate < 0.6):
            health = "degraded"
        elif failed:
            health = "watch"
        else:
            health = "healthy"
        latest = values[-1] if values else None
        latest_failure = next(
            (item for item in reversed(values) if item.get("success") is False),
            None,
        )
        return {
            "capability": capability,
            "health": health,
            "observations": len(values),
            "terminal_observations": total,
            "succeeded": succeeded,
            "failed": failed,
            "success_rate": success_rate,
            "consecutive_failures": consecutive_failures,
            "latest": dict(latest) if latest else None,
            "latest_failure": dict(latest_failure) if latest_failure else None,
        }

    def snapshot(self, *, evidence_limit: int = 500, recent_limit: int = 20) -> dict[str, Any]:
        evidence = self.evidence(evidence_limit)
        groups: dict[str, list[dict[str, Any]]] = {}
        for item in evidence:
            groups.setdefault(str(item.get("capability") or "unknown"), []).append(item)
        scorecards = {
            capability: self._scorecard(capability, values)
            for capability, values in sorted(groups.items())
        }
        return {
            "schema_version": 1,
            "observed_at": _now_iso(),
            "consciousness_claim": False,
            "evidence_count": len(evidence),
            "scorecards": scorecards,
            "recent_evidence": [dict(item) for item in reversed(evidence[-max(0, int(recent_limit)) :])],
        }


    def roadmap(
        self,
        intentions: list[dict[str, Any]],
        *,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Rank open intentions using priority, evidence, owner alignment and state."""

        priority_score = {"P0": 400, "P1": 300, "P2": 200, "P3": 100}
        status_score = {
            "active": 45,
            "planned": 35,
            "proposed": 25,
            "blocked": -40,
            "awaiting_promotion": 5,
        }
        rows: list[dict[str, Any]] = []
        for item in intentions:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status", "proposed"))
            if status in {"completed", "dismissed"}:
                continue
            evidence = item.get("evidence", [])
            evidence_count = len(evidence) if isinstance(evidence, list) else 0
            attempts = int(item.get("attempts", 0) or 0)
            score = (
                priority_score.get(str(item.get("priority", "P2")), 0)
                + status_score.get(status, 0)
                + (30 if item.get("owner_aligned") else 0)
                + min(evidence_count, 10) * 3
                - min(attempts, 10) * 4
            )
            if status == "awaiting_promotion":
                action = "检查宿主机晋升证据或拒绝原因"
            elif status == "blocked":
                action = "分析失败证据，缩小范围并重新计划"
            elif status == "active":
                action = "继续当前受控循环并收集测试证据"
            elif status == "planned":
                action = "经主人确认后进入 app-tmp 沙盒"
            else:
                action = "先生成计划和验收证据定义"
            rows.append(
                {
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "priority": item.get("priority"),
                    "status": status,
                    "score": score,
                    "evidence_count": evidence_count,
                    "attempts": attempts,
                    "owner_aligned": bool(item.get("owner_aligned")),
                    "recommended_action": action,
                }
            )
        rows.sort(key=lambda item: (-int(item["score"]), str(item.get("id", ""))))
        snapshot = self.snapshot()
        return {
            "generated_at": _now_iso(),
            "consciousness_claim": False,
            "recommended": rows[0] if rows else None,
            "intentions": rows[: max(0, min(int(limit), 50))],
            "capability_scorecards": snapshot["scorecards"],
        }

    def findings(self) -> list[dict[str, str]]:
        snapshot = self.snapshot()
        findings: list[dict[str, str]] = []
        for capability, card in snapshot["scorecards"].items():
            if card["health"] == "degraded":
                failure = card.get("latest_failure") or {}
                findings.append(
                    {
                        "priority": "P1",
                        "code": f"capability_degraded:{capability}",
                        "finding": (
                            f"能力 {capability} 的可信结果显示连续失败 "
                            f"{card['consecutive_failures']} 次，成功率 {card['success_rate']}"
                        ),
                        "recommendation": (
                            f"分析并修复软件验证失败：{failure.get('target', 'unknown')}"
                            if capability == "software.validation"
                            else (
                                f"分析并修复能力 {capability} 的最近失败："
                                f"{failure.get('target', 'unknown')} / {failure.get('summary', '')}"
                            )
                        ),
                    }
                )
            elif card["health"] == "watch":
                findings.append(
                    {
                        "priority": "P2",
                        "code": f"capability_watch:{capability}",
                        "finding": f"能力 {capability} 已出现失败，但尚未形成连续退化",
                        "recommendation": (
                            f"分析并修复软件验证失败："
                            f"{(card.get('latest_failure') or {}).get('target', 'unknown')}"
                            if capability == "software.validation"
                            else f"补充能力 {capability} 的回归验证并观察后续结果"
                        ),
                    }
                )
        findings.sort(key=lambda item: (item["priority"], item["code"]))
        return findings
