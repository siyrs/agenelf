"""Persistent operational self-model, reflection sedimentation and improvement intentions.

This module implements engineering continuity rather than subjective consciousness.
Agenelf can preserve observations about its own runtime, turn evidence into bounded
reflection records, maintain explicit improvement intentions, and route an intention
into the existing sandboxed autonomy pipeline.  The records live under
``local/self/`` so upgrades to shared ``app/`` code do not erase owner-specific
continuity.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .privacy import redact_sensitive_text, sanitize_value

_SCHEMA_VERSION = 1
_VALID_PRIORITIES = {"P0", "P1", "P2", "P3"}
_VALID_STATUSES = {
    "proposed",
    "planned",
    "active",
    "awaiting_promotion",
    "blocked",
    "completed",
    "dismissed",
}
_OPEN_STATUSES = {
    "proposed",
    "planned",
    "active",
    "awaiting_promotion",
    "blocked",
}
_PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
_COMMITMENT = {"P0": 100, "P1": 80, "P2": 60, "P3": 40}
_ID_RE = re.compile(r"(?:intent|reflection)-[A-Za-z0-9._-]+")
_PURPOSE = "持续理解主人目标，以证据完成任务，并在安全边界内改进通用能力"
_PRINCIPLES = (
    "主人目标与明确授权优先",
    "证据优先于自我宣称",
    "安全边界优先于速度",
    "把失败与结果沉淀为可验证的下一步",
    "通用能力进入 app，个性化连续性留在 local",
)


class SelfDevelopmentError(RuntimeError):
    """Expected failure in the persistent self-development subsystem."""


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


def _nonnegative_int(value: object, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, int(default))


def _fingerprint_title(title: str) -> str:
    normalized = " ".join(title.lower().split())
    import hashlib

    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class SelfDevelopmentStore:
    """Atomic, bounded owner-local continuity store."""

    def __init__(
        self,
        self_dir: str | Path,
        *,
        max_reflections: int = 200,
        max_intentions: int = 100,
        prompt_max_chars: int = 4000,
        auto_reflect_every_episodes: int = 12,
        min_reflection_interval_seconds: int = 3600,
    ):
        self.self_dir = Path(self_dir).resolve()
        self.reflections_path = self.self_dir / "reflections.json"
        self.intentions_path = self.self_dir / "intentions.json"
        self.state_path = self.self_dir / "state.json"
        self.max_reflections = max(1, int(max_reflections))
        self.max_intentions = max(1, int(max_intentions))
        self.prompt_max_chars = max(0, int(prompt_max_chars))
        self.auto_reflect_every_episodes = max(0, int(auto_reflect_every_episodes))
        self.min_reflection_interval_seconds = max(
            0, int(min_reflection_interval_seconds)
        )
        self.self_dir.mkdir(parents=True, exist_ok=True)
        self.reflections = self._load_records(self.reflections_path, "reflection")
        self.intentions = self._load_records(self.intentions_path, "intent")
        self.state = self._load_state()
        self._trim_and_save_if_needed()

    @staticmethod
    def _load_records(path: Path, prefix: str) -> list[dict[str, Any]]:
        value = _read_json(path, [])
        if not isinstance(value, list):
            return []
        records: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            sanitized = sanitize_value(item)
            if not isinstance(sanitized, dict):
                continue
            record_id = str(sanitized.get("id", ""))
            if record_id.startswith(prefix + "-"):
                records.append(sanitized)
        return records

    def _load_state(self) -> dict[str, Any]:
        value = _read_json(self.state_path, {})
        if not isinstance(value, dict):
            value = {}
        created_at = _safe_text(value.get("created_at") or _now_iso(), 100)
        continuity_id = _safe_text(value.get("continuity_id"), 100)
        if not re.fullmatch(r"self-[A-Za-z0-9._-]+", continuity_id):
            continuity_id = f"self-{uuid.uuid4().hex}"
        return {
            "schema_version": _SCHEMA_VERSION,
            "continuity_id": continuity_id,
            "created_at": created_at,
            "updated_at": _now_iso(),
            "last_reflection_at": value.get("last_reflection_at"),
            "episode_cursor": _nonnegative_int(value.get("episode_cursor", 0)),
            "operational_identity": {
                "kind": "persistent tool-using software agent",
                "purpose": _PURPOSE,
                "principles": list(_PRINCIPLES),
                "consciousness_claim": False,
            },
        }

    def _trim_and_save_if_needed(self) -> None:
        self.reflections = self.reflections[-self.max_reflections :]
        if len(self.intentions) > self.max_intentions:
            terminal = [
                item
                for item in self.intentions
                if item.get("status") in {"completed", "dismissed"}
            ]
            open_items = [
                item for item in self.intentions if item.get("status") in _OPEN_STATUSES
            ]
            keep_terminal = max(0, self.max_intentions - len(open_items))
            self.intentions = (terminal[-keep_terminal:] if keep_terminal else []) + open_items
            self.intentions = self.intentions[-self.max_intentions :]
        # Always rewrite normalized records so manually inserted credential-like
        # values are redacted before they can be exposed through prompts or APIs.
        _atomic_json(self.reflections_path, self.reflections)
        _atomic_json(self.intentions_path, self.intentions)
        self._save_state()

    def _save_state(self) -> None:
        self.state["updated_at"] = _now_iso()
        _atomic_json(self.state_path, self.state)

    def _save_reflections(self) -> None:
        self.reflections = self.reflections[-self.max_reflections :]
        _atomic_json(self.reflections_path, self.reflections)

    def _save_intentions(self) -> None:
        if len(self.intentions) > self.max_intentions:
            self.intentions = self.intentions[-self.max_intentions :]
        _atomic_json(self.intentions_path, self.intentions)

    def record_reflection(
        self,
        *,
        trigger: str,
        summary: str,
        observations: list[str],
        lessons: list[str],
        evidence: list[str] | None = None,
        generated_intention_ids: list[str] | None = None,
        episode_cursor: int | None = None,
        deep: bool = False,
        deep_warning: str = "",
    ) -> dict[str, Any]:
        reflection = {
            "schema_version": _SCHEMA_VERSION,
            "id": f"reflection-{_now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}",
            "at": _now_iso(),
            "trigger": _safe_text(trigger, 100) or "manual",
            "summary": _safe_text(summary, 2000),
            "observations": _safe_strings(observations, limit=20, item_limit=1200),
            "lessons": _safe_strings(lessons, limit=20, item_limit=1200),
            "evidence": _safe_strings(evidence or [], limit=20, item_limit=1200),
            "generated_intention_ids": [
                value
                for value in (generated_intention_ids or [])[:20]
                if isinstance(value, str) and value.startswith("intent-")
            ],
            "deep_reflection": bool(deep),
            "deep_warning": _safe_text(deep_warning, 1000),
            "consciousness_claim": False,
        }
        self.reflections.append(reflection)
        self._save_reflections()
        self.state["last_reflection_at"] = reflection["at"]
        if episode_cursor is not None:
            self.state["episode_cursor"] = max(0, int(episode_cursor))
        self._save_state()
        return dict(reflection)

    def recent_reflections(self, limit: int = 10) -> list[dict[str, Any]]:
        count = max(0, min(int(limit), 50))
        return [dict(item) for item in reversed(self.reflections[-count:])]

    def create_intention(
        self,
        *,
        title: str,
        rationale: str = "",
        priority: str = "P2",
        acceptance_criteria: list[str] | None = None,
        evidence: list[str] | None = None,
        source: str = "manual",
        owner_aligned: bool = False,
    ) -> tuple[dict[str, Any], bool]:
        safe_title = _safe_text(title, 300)
        if not safe_title:
            raise SelfDevelopmentError("改进意向标题不能为空")
        priority = str(priority or "P2").upper()
        if priority not in _VALID_PRIORITIES:
            raise SelfDevelopmentError(
                f"未知优先级 {priority!r}；应为 {', '.join(sorted(_VALID_PRIORITIES))}"
            )
        fingerprint = _fingerprint_title(safe_title)
        for intention in self.intentions:
            if (
                intention.get("fingerprint") == fingerprint
                and intention.get("status") in _OPEN_STATUSES
            ):
                return dict(intention), False
        now = _now_iso()
        intention = {
            "schema_version": _SCHEMA_VERSION,
            "id": f"intent-{_now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}",
            "title": safe_title,
            "rationale": _safe_text(rationale, 2000),
            "priority": priority,
            "status": "proposed",
            "operational_commitment": _COMMITMENT[priority],
            "acceptance_criteria": _safe_strings(
                acceptance_criteria or [], limit=12, item_limit=800
            )
            or [
                "有明确、可复现的验收证据",
                "不绕过权限、审批、测试和晋升安全门",
            ],
            "evidence": _safe_strings(evidence or [], limit=20, item_limit=1000),
            "source": _safe_text(source, 100) or "manual",
            "owner_aligned": bool(owner_aligned),
            "fingerprint": fingerprint,
            "created_at": now,
            "updated_at": now,
            "attempts": 0,
            "last_note": "",
            "linked_cycle_id": None,
            "evolution_session_id": None,
            "consciousness_claim": False,
        }
        self.intentions.append(intention)
        self._save_intentions()
        return dict(intention), True

    def get_intention(self, intention_id: str) -> dict[str, Any]:
        if not _ID_RE.fullmatch(intention_id or "") or not intention_id.startswith(
            "intent-"
        ):
            raise SelfDevelopmentError(f"非法改进意向 ID：{intention_id!r}")
        for intention in self.intentions:
            if intention.get("id") == intention_id:
                return dict(intention)
        raise SelfDevelopmentError(f"改进意向不存在：{intention_id}")

    def list_intentions(
        self, status: str = "", limit: int = 20
    ) -> list[dict[str, Any]]:
        normalized_status = str(status or "").strip()
        if normalized_status and normalized_status not in _VALID_STATUSES:
            raise SelfDevelopmentError(f"未知意向状态：{normalized_status}")
        values = [
            item
            for item in self.intentions
            if not normalized_status or item.get("status") == normalized_status
        ]
        values.sort(key=lambda item: str(item.get("updated_at", "")), reverse=True)
        values.sort(
            key=lambda item: _PRIORITY_RANK.get(str(item.get("priority")), 9)
        )
        return [dict(item) for item in values[: max(0, min(int(limit), 100))]]

    def update_intention(
        self,
        intention_id: str,
        *,
        status: str | None = None,
        note: str = "",
        linked_cycle_id: str | None = None,
        evolution_session_id: str | None = None,
        increment_attempts: bool = False,
    ) -> dict[str, Any]:
        if status is not None and status not in _VALID_STATUSES:
            raise SelfDevelopmentError(f"未知意向状态：{status}")
        for index, intention in enumerate(self.intentions):
            if intention.get("id") != intention_id:
                continue
            updated = dict(intention)
            if status is not None:
                current = str(updated.get("status", "proposed"))
                if current in {"completed", "dismissed"} and status != current:
                    raise SelfDevelopmentError(
                        f"终态意向 {intention_id} 不能从 {current} 变更为 {status}"
                    )
                updated["status"] = status
            if note:
                updated["last_note"] = _safe_text(note, 2000)
            if linked_cycle_id is not None:
                updated["linked_cycle_id"] = (
                    _safe_text(linked_cycle_id, 100) or None
                )
            if evolution_session_id is not None:
                updated["evolution_session_id"] = (
                    _safe_text(evolution_session_id, 100) or None
                )
            if increment_attempts:
                updated["attempts"] = _nonnegative_int(updated.get("attempts", 0)) + 1
            updated["updated_at"] = _now_iso()
            self.intentions[index] = updated
            self._save_intentions()
            return dict(updated)
        raise SelfDevelopmentError(f"改进意向不存在：{intention_id}")

    def reflection_due(self, episode_count: int) -> bool:
        if self.auto_reflect_every_episodes <= 0:
            return False
        cursor = _nonnegative_int(self.state.get("episode_cursor", 0))
        if _nonnegative_int(episode_count) - cursor < self.auto_reflect_every_episodes:
            return False
        last = _parse_iso(self.state.get("last_reflection_at"))
        if last is None:
            return True
        elapsed = (_now() - last).total_seconds()
        return elapsed >= self.min_reflection_interval_seconds

    def status(self) -> dict[str, Any]:
        counts = {status: 0 for status in sorted(_VALID_STATUSES)}
        for intention in self.intentions:
            status = str(intention.get("status", "proposed"))
            if status in counts:
                counts[status] += 1
        open_items = [
            item for item in self.list_intentions(limit=100) if item.get("status") in _OPEN_STATUSES
        ][:5]
        latest = self.reflections[-1] if self.reflections else None
        return {
            "schema_version": _SCHEMA_VERSION,
            "self_dir": str(self.self_dir),
            "continuity_id": self.state["continuity_id"],
            "operational_identity": dict(self.state["operational_identity"]),
            "last_reflection_at": self.state.get("last_reflection_at"),
            "episode_cursor": self.state.get("episode_cursor", 0),
            "reflection_count": len(self.reflections),
            "latest_reflection": dict(latest) if isinstance(latest, dict) else None,
            "intention_count": len(self.intentions),
            "intention_status_counts": counts,
            "open_intentions": [dict(item) for item in open_items],
            "policy": {
                "auto_reflect_every_episodes": self.auto_reflect_every_episodes,
                "min_reflection_interval_seconds": self.min_reflection_interval_seconds,
                "max_reflections": self.max_reflections,
                "max_intentions": self.max_intentions,
                "auto_pursue": False,
                "consciousness_claim": False,
            },
        }

    def prompt_block(self) -> str:
        status = self.status()
        lines = [
            "这是可持久化的软件成长状态，不是情感、欲望或主观意识。",
            f"- 连续性 ID：{status['continuity_id']}",
            f"- 目的：{status['operational_identity']['purpose']}",
        ]
        latest = status.get("latest_reflection")
        if isinstance(latest, dict):
            lines.append(f"- 最近沉淀：{_safe_text(latest.get('summary', ''), 1000)}")
            for lesson in latest.get("lessons", [])[:3]:
                lines.append(f"    - 教训：{_safe_text(lesson, 800)}")
        intentions = status.get("open_intentions", [])
        if intentions:
            lines.append("- 当前改进意向：")
            for intention in intentions:
                lines.append(
                    f"    - {intention.get('id')} | {intention.get('priority')} | "
                    f"{intention.get('status')} | 承诺度={intention.get('operational_commitment')} | "
                    f"{_safe_text(intention.get('title'), 500)}"
                )
        else:
            lines.append("- 当前没有开放的改进意向。")
        text = "\n".join(lines)
        if len(text) > self.prompt_max_chars:
            text = text[: max(0, self.prompt_max_chars - 1)] + "…"
        return text


class SelfDevelopmentEngine:
    """Build evidence-based reflections and connect intentions to autonomy cycles."""

    def __init__(self, agent: Any, root: str | Path | None = None):
        self.agent = agent
        configured_root = root or agent.config.get("runtime_root") or os.environ.get(
            "AGENELF_ROOT"
        )
        self.root = (
            Path(configured_root).resolve()
            if configured_root
            else Path(__file__).resolve().parents[2]
        )
        self.local_dir = Path(
            agent.config.get("local_dir")
            or os.environ.get("AGENELF_LOCAL_DIR")
            or self.root / "local"
        ).resolve()
        cfg = agent.config.get("self_development", {})
        if not isinstance(cfg, dict):
            cfg = {}
        self.deep_reflection_enabled = bool(cfg.get("allow_llm_reflection", True))
        self.store = SelfDevelopmentStore(
            agent.config.get("self_dir") or self.local_dir / "self",
            max_reflections=int(cfg.get("max_reflections", 200)),
            max_intentions=int(cfg.get("max_intentions", 100)),
            prompt_max_chars=int(cfg.get("prompt_max_chars", 4000)),
            auto_reflect_every_episodes=int(
                cfg.get("auto_reflect_every_episodes", 12)
            ),
            min_reflection_interval_seconds=int(
                cfg.get("min_reflection_interval_seconds", 3600)
            ),
        )

    def summary_for_snapshot(self) -> dict[str, Any]:
        self.reconcile()
        status = self.store.status()
        return {
            "continuity_id": status["continuity_id"],
            "last_reflection_at": status["last_reflection_at"],
            "reflection_count": status["reflection_count"],
            "intention_status_counts": status["intention_status_counts"],
            "open_intentions": status["open_intentions"],
            "consciousness_claim": False,
        }

    def status(self) -> dict[str, Any]:
        self.reconcile()
        return self.store.status()

    def prompt_block(self) -> str:
        self.reconcile()
        return self.store.prompt_block()

    def recent_reflections(self, limit: int = 10) -> list[dict[str, Any]]:
        return self.store.recent_reflections(limit)

    def list_intentions(self, status: str = "", limit: int = 20) -> list[dict[str, Any]]:
        self.reconcile()
        return self.store.list_intentions(status=status, limit=limit)

    def get_intention(self, intention_id: str) -> dict[str, Any]:
        self.reconcile()
        return self.store.get_intention(intention_id)

    def create_intention(
        self,
        *,
        title: str,
        rationale: str = "",
        priority: str = "P2",
        acceptance_criteria: list[str] | None = None,
        evidence: list[str] | None = None,
        source: str = "manual",
        owner_aligned: bool = True,
    ) -> dict[str, Any]:
        intention, created = self.store.create_intention(
            title=title,
            rationale=rationale,
            priority=priority,
            acceptance_criteria=acceptance_criteria,
            evidence=evidence,
            source=source,
            owner_aligned=owner_aligned,
        )
        return {"created": created, "intention": intention}

    def _autonomy(self):
        from .autonomy import AutonomyEngine

        return AutonomyEngine(self.agent, root=self.root)

    def _deep_payload(
        self,
        snapshot: dict[str, Any],
        assessment: dict[str, Any],
        note: str,
    ) -> tuple[dict[str, Any] | None, str]:
        if not self.deep_reflection_enabled:
            return None, "配置禁止 LLM 深度反思，已使用确定性反思"
        recent = []
        for memory in getattr(self.agent.memory, "memories", [])[-12:]:
            if isinstance(memory, dict):
                recent.append(
                    {
                        "kind": str(memory.get("kind", "")),
                        "content": _safe_text(memory.get("content", ""), 800),
                    }
                )
        messages = [
            {
                "role": "system",
                "content": (
                    "你是软件系统复盘器。只分析可观测证据，不得声称主观意识、"
                    "情感或自由意志。仅输出一个 JSON 对象。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": (
                            "生成一次简洁复盘。字段：summary 字符串；observations 字符串数组；"
                            "lessons 字符串数组；intentions 数组，每项含 title、rationale、"
                            "priority(P0-P3)、acceptance_criteria。不要输出凭据。"
                        ),
                        "note": _safe_text(note, 1000),
                        "snapshot": sanitize_value(snapshot),
                        "assessment": sanitize_value(assessment),
                        "recent_memory": recent,
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        response = self.agent.llm.chat(messages, tools=None)
        content = str((response or {}).get("content") or "").strip()
        if content.startswith("```"):
            lines = content.splitlines()
            if lines:
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            content = "\n".join(lines).strip()
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return None, "LLM 深度反思未返回有效 JSON，已使用确定性反思"
        if not isinstance(payload, dict):
            return None, "LLM 深度反思顶层不是对象，已使用确定性反思"
        sanitized = sanitize_value(payload)
        return (sanitized if isinstance(sanitized, dict) else None), ""

    def reflect(
        self,
        *,
        trigger: str = "manual",
        note: str = "",
        deep: bool = False,
    ) -> dict[str, Any]:
        autonomy = self._autonomy()
        snapshot = autonomy.snapshot()
        assessment = autonomy.assess(snapshot)
        memory_stats = self.agent.memory.stats()
        local_status = self.agent.local_status()
        findings = [
            item
            for item in assessment.get("findings", [])
            if isinstance(item, dict)
        ]
        observations = [
            f"运行健康状态：{assessment.get('health', 'unknown')}",
            f"已加载能力域：{snapshot.get('capability_count', 0)}；技能：{snapshot.get('skill_count', 0)}",
            f"技能或运行时错误：{len(snapshot.get('registry_errors', {}))}",
            f"长期记忆条目：{memory_stats.get('entries', 0)}/{memory_stats.get('max_entries', 0)}",
            f"local 上下文警告：{len(local_status.get('warnings', []))}",
        ]
        observations.extend(
            f"{item.get('priority')} {item.get('code')}: {item.get('finding')}"
            for item in findings[:5]
        )
        if note.strip():
            observations.append(f"主人或运行时补充：{_safe_text(note, 1000)}")
        lessons = [
            _safe_text(item.get("recommendation", ""), 1000)
            for item in findings[:5]
            if _safe_text(item.get("recommendation", ""), 1000)
        ]
        if not lessons:
            lessons = ["继续选择小而可验证的改进点，并保留测试与执行证据"]

        deep_payload: dict[str, Any] | None = None
        deep_warning = ""
        if deep:
            deep_payload, deep_warning = self._deep_payload(snapshot, assessment, note)
            if deep_payload:
                summary_override = _safe_text(deep_payload.get("summary", ""), 2000)
                if summary_override:
                    summary = summary_override
                else:
                    summary = ""
                observations.extend(
                    _safe_strings(
                        deep_payload.get("observations", []), limit=10, item_limit=1000
                    )
                )
                lessons.extend(
                    _safe_strings(
                        deep_payload.get("lessons", []), limit=10, item_limit=1000
                    )
                )
            else:
                summary = ""
        else:
            summary = ""

        if not summary:
            top = findings[0] if findings else {}
            summary = (
                f"完成一次{_safe_text(trigger, 80) or 'manual'}反思："
                f"当前状态为 {assessment.get('health', 'unknown')}；"
                f"最高优先级改进点是 {top.get('finding', '持续小步改进')}。"
            )

        created_ids: list[str] = []
        for item in findings[:3]:
            title = _safe_text(item.get("recommendation", ""), 300)
            if not title:
                continue
            result = self.create_intention(
                title=title,
                rationale=str(item.get("finding", "")),
                priority=str(item.get("priority", "P2")),
                acceptance_criteria=[
                    "相关行为有自动化测试或可验证证据",
                    "变更通过完整测试和宿主机安全门",
                    "不写入或泄露主人私有配置与凭据",
                ],
                evidence=[f"assessment:{item.get('code', 'unknown')}"],
                source=f"reflection:{trigger}",
                owner_aligned=False,
            )
            if result["created"]:
                created_ids.append(result["intention"]["id"])

        if deep_payload:
            for raw in deep_payload.get("intentions", [])[:3]:
                if not isinstance(raw, dict):
                    continue
                try:
                    result = self.create_intention(
                        title=str(raw.get("title", "")),
                        rationale=str(raw.get("rationale", "")),
                        priority=str(raw.get("priority", "P2")),
                        acceptance_criteria=_safe_strings(
                            raw.get("acceptance_criteria", []),
                            limit=10,
                            item_limit=800,
                        ),
                        evidence=["deep_reflection"],
                        source=f"deep_reflection:{trigger}",
                        owner_aligned=False,
                    )
                except SelfDevelopmentError:
                    continue
                if result["created"]:
                    created_ids.append(result["intention"]["id"])

        evidence = [
            f"snapshot:{snapshot.get('observed_at', '')}",
            f"memory_entries:{memory_stats.get('entries', 0)}",
            f"local_fingerprint:{local_status.get('fingerprint', '')}",
        ]
        reflection = self.store.record_reflection(
            trigger=trigger,
            summary=summary,
            observations=observations,
            lessons=lessons,
            evidence=evidence,
            generated_intention_ids=created_ids,
            episode_cursor=int(memory_stats.get("kinds", {}).get("episode", 0)),
            deep=bool(deep and deep_payload),
            deep_warning=deep_warning,
        )
        return {
            "reflection": reflection,
            "created_intention_ids": created_ids,
            "development": self.store.status(),
        }

    def maybe_reflect(self, trigger: str = "conversation") -> dict[str, Any] | None:
        episode_count = int(
            self.agent.memory.stats().get("kinds", {}).get("episode", 0)
        )
        if not self.store.reflection_due(episode_count):
            return None
        return self.reflect(
            trigger=trigger,
            note=(
                f"达到自动沉淀阈值；自上次游标以来累计 "
                f"{episode_count - int(self.store.state.get('episode_cursor', 0))} 条对话事件"
            ),
            deep=False,
        )

    def pursue_intention(
        self, intention_id: str, *, apply_changes: bool = False
    ) -> dict[str, Any]:
        intention = self.store.get_intention(intention_id)
        if intention.get("status") in {"completed", "dismissed"}:
            raise SelfDevelopmentError(
                f"意向 {intention_id} 已处于终态 {intention.get('status')}"
            )
        initial_status = "active" if apply_changes else "planned"
        intention = self.store.update_intention(
            intention_id,
            status=initial_status,
            note="开始生成受控改进计划" if not apply_changes else "开始沙盒改进",
            increment_attempts=True,
        )
        criteria = "\n".join(
            f"- {item}" for item in intention.get("acceptance_criteria", [])
        )
        goal = (
            f"{intention.get('title')}\n"
            f"原因：{intention.get('rationale')}\n"
            f"验收条件：\n{criteria}"
        )
        cycle = self._autonomy().run_cycle(
            goal=goal,
            apply_changes=apply_changes,
        )
        cycle["source_intention_id"] = intention_id
        cycle_id = str(cycle.get("id", ""))
        if cycle_id:
            _atomic_json(
                self.root / "data" / "autonomy-cycles" / f"{cycle_id}.json",
                cycle,
            )
        session_id = str(cycle.get("evolution_session_id", "") or "")
        status = str(cycle.get("status", "failed"))
        if status == "promotion_requested":
            intention_status = "awaiting_promotion"
        elif status == "plan_ready":
            intention_status = "planned"
        elif status in {"blocked", "failed"}:
            intention_status = "blocked"
        else:
            intention_status = "active"
        intention = self.store.update_intention(
            intention_id,
            status=intention_status,
            note=f"自主循环状态：{status}",
            linked_cycle_id=cycle_id,
            evolution_session_id=session_id,
        )
        self.store.record_reflection(
            trigger="intention_pursuit",
            summary=f"推进改进意向「{intention.get('title')}」，自主循环状态为 {status}。",
            observations=[
                f"意向：{intention_id}",
                f"循环：{cycle_id or 'unknown'}",
                f"结果：{status}",
            ],
            lessons=[
                "只有测试、gate 与宿主机晋升证据完整后，才能把意向标记为完成"
            ],
            evidence=[f"autonomy_cycle:{cycle_id}"] if cycle_id else [],
            generated_intention_ids=[],
            episode_cursor=int(
                self.agent.memory.stats().get("kinds", {}).get("episode", 0)
            ),
        )
        return {"intention": intention, "cycle": cycle}

    def observe_cycle(self, cycle: dict[str, Any]) -> None:
        if not isinstance(cycle, dict):
            return
        intention_id = str(cycle.get("source_intention_id", "") or "")
        status = str(cycle.get("status", ""))
        if intention_id:
            try:
                mapped = {
                    "plan_ready": "planned",
                    "promotion_requested": "awaiting_promotion",
                    "failed": "blocked",
                    "blocked": "blocked",
                }.get(status)
                if mapped:
                    self.store.update_intention(
                        intention_id,
                        status=mapped,
                        note=f"观察到自主循环状态：{status}",
                        linked_cycle_id=str(cycle.get("id", "") or ""),
                        evolution_session_id=str(
                            cycle.get("evolution_session_id", "") or ""
                        ),
                    )
            except SelfDevelopmentError:
                pass
        if status not in {"promotion_requested", "failed", "blocked"}:
            return
        error = _safe_text(cycle.get("error", ""), 500)
        created_ids: list[str] = []
        if not intention_id and status in {"failed", "blocked"}:
            try:
                created = self.create_intention(
                    title="分析并修复最近一次自主循环失败",
                    rationale=error or "自主循环未能完成",
                    priority="P1",
                    acceptance_criteria=[
                        "复现失败",
                        "增加回归测试",
                        "新候选通过完整安全门",
                    ],
                    evidence=[f"autonomy_cycle:{cycle.get('id', '')}"],
                    source="autonomy_outcome",
                    owner_aligned=False,
                )
                if created["created"]:
                    created_ids.append(created["intention"]["id"])
            except SelfDevelopmentError:
                pass
        summary = (
            "自主候选已通过测试并等待宿主机审查晋升"
            if status == "promotion_requested"
            else f"自主循环停留在 {status}，需要根据证据修正后再尝试"
        )
        self.store.record_reflection(
            trigger="autonomy_outcome",
            summary=summary,
            observations=[
                f"循环：{cycle.get('id', 'unknown')}",
                f"目标：{_safe_text(cycle.get('goal', ''), 500)}",
                f"状态：{status}",
                f"错误：{error}" if error else "错误：无",
            ],
            lessons=[
                (
                    "候选仍不能视为完成，必须等待不可变宿主机晋升证据"
                    if status == "promotion_requested"
                    else "缩小改动、补充测试并保留失败证据"
                )
            ],
            evidence=[f"autonomy_cycle:{cycle.get('id', '')}"],
            generated_intention_ids=created_ids,
            episode_cursor=int(
                self.agent.memory.stats().get("kinds", {}).get("episode", 0)
            ),
        )

    def reconcile(self) -> int:
        changed = 0
        for intention in list(self.store.intentions):
            if intention.get("status") != "awaiting_promotion":
                continue
            intention_id = str(intention.get("id", ""))
            session_id = str(intention.get("evolution_session_id", "") or "")
            if not session_id:
                continue
            history = self.root / "data" / "promotion-history" / session_id
            request = self.root / "data" / "promote-requests" / session_id
            try:
                if history.is_dir():
                    self.store.update_intention(
                        intention_id,
                        status="completed",
                        note=f"检测到不可变晋升证据：{history}",
                    )
                    changed += 1
                elif (request / "REJECTED").is_file():
                    reason = _safe_text(
                        (request / "REJECTED").read_text(encoding="utf-8"), 1000
                    )
                    self.store.update_intention(
                        intention_id,
                        status="blocked",
                        note=reason or "晋升请求被拒绝",
                    )
                    changed += 1
            except (OSError, SelfDevelopmentError):
                continue
        return changed
