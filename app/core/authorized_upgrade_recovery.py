"""Bounded recovery for owner-authorized candidate generation.

The first owner approval authorizes one bounded upgrade session, not one fragile HTTP
request. After that intent is consumed, transient model failures or failed candidate
tests may be retried within the exact same approved scopes and limits. A tested
candidate still requires its own second exact owner approval.
"""
from __future__ import annotations

import os
from typing import Any, Callable

from core.privacy import redact_sensitive_text

_DEFAULT_MAX_ATTEMPTS = 3
_MAX_ATTEMPTS_LIMIT = 6
_RETRIABLE_GENERATION_STATUSES = {
    "generating",
    "generation_failed",
    "tests_failed",
    "candidate_denied",
}


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def configured_attempts(agent: Any) -> int:
    config = getattr(agent, "config", {})
    autonomy = config.get("autonomy", {}) if isinstance(config, dict) else {}
    upgrade = autonomy.get("owner_authorized_upgrade", {}) if isinstance(autonomy, dict) else {}
    raw = os.environ.get(
        "AGENELF_AUTHORIZED_UPGRADE_GENERATION_ATTEMPTS",
        str(upgrade.get("max_generation_attempts", _DEFAULT_MAX_ATTEMPTS))
        if isinstance(upgrade, dict)
        else str(_DEFAULT_MAX_ATTEMPTS),
    )
    return _bounded_int(raw, _DEFAULT_MAX_ATTEMPTS, 1, _MAX_ATTEMPTS_LIMIT)


def _safe_error(exc: BaseException) -> str:
    return redact_sensitive_text(f"{type(exc).__name__}: {exc}")[:4000]


def _failure_evidence(session: dict[str, Any]) -> str:
    values: list[str] = []
    if session.get("last_generation_error"):
        values.append(str(session["last_generation_error"]))
    test_result = session.get("test_result")
    if isinstance(test_result, dict):
        values.append(str(test_result.get("output", ""))[-6000:])
    return redact_sensitive_text("\n".join(values))[-8000:]


def _candidate_summary(session: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for record in session.get("changed_file_records") or []:
        if not isinstance(record, dict):
            continue
        rows.append(
            {
                "path": str(record.get("path", "")),
                "kind": "created" if record.get("created") else "modified",
                "changed_lines": int(record.get("changed_lines", 0) or 0),
                "before_sha256": str(record.get("before_sha256", ""))[:16],
                "after_sha256": str(record.get("after_sha256", ""))[:16],
            }
        )
    return rows


def install(module: Any) -> None:
    """Install bounded retry/public-status wrappers exactly once."""

    if getattr(module, "_agenelf_authorized_upgrade_recovery_installed", False):
        return
    original_generate: Callable[..., dict[str, Any]] = module._generate_candidate
    original_advance: Callable[..., dict[str, Any]] = module.advance_session
    original_build_prompt: Callable[..., str] = module._build_prompt
    original_public_status: Callable[..., dict[str, Any]] = module.public_status

    def build_prompt(session: dict[str, Any], context: dict[str, str]) -> str:
        prompt = original_build_prompt(session, context)
        attempts = int(session.get("generation_attempts", 0) or 0)
        evidence = _failure_evidence(session)
        if attempts > 1 and evidence:
            prompt += (
                "\n\n【上一候选失败证据】\n"
                + evidence
                + "\n请修复实现根因；不得修改既有测试、缩小授权红线或重复上一候选。"
            )
        return prompt

    def generate(agent: Any, session: dict[str, Any]) -> dict[str, Any]:
        current = module.load_session(str(session["id"]))
        limit = configured_attempts(agent)
        attempt = int(current.get("generation_attempts", 0) or 0) + 1
        if attempt > limit:
            current["status"] = "failed"
            current["error"] = (
                f"候选生成已达到有界重试上限 {limit}；原升级意图没有扩大，"
                "需要主人检查失败证据后创建新会话。"
            )
            current["next_action"] = "review_failure_and_start_new_upgrade"
            module.save_session(current)
            return current
        current["generation_attempts"] = attempt
        current["max_generation_attempts"] = limit
        current["status"] = "generating"
        current.pop("error", None)
        module.save_session(current)
        try:
            return original_generate(agent, current)
        except Exception as exc:
            latest = module.load_session(str(current["id"]))
            if latest.get("status") != "tests_failed":
                latest["status"] = "generation_failed"
            latest["last_generation_error"] = _safe_error(exc)
            latest["generation_attempts"] = attempt
            latest["max_generation_attempts"] = limit
            latest["next_action"] = f"/upgrade {latest['id']}"
            module.save_session(latest)
            return latest

    def reissue_candidate_authorization(session: dict[str, Any]) -> dict[str, Any]:
        binding = session.get("candidate_binding")
        if not isinstance(binding, dict):
            session["status"] = "candidate_denied"
            session["error"] = "候选授权已失效且候选绑定缺失，需要重新生成候选"
            module.save_session(session)
            return session
        module._request_candidate_approval(session, binding)
        session["status"] = "awaiting_candidate_approval"
        session["next_action"] = f"/approve {session.get('candidate_auth_id', '')}"
        module.save_session(session)
        return session

    def advance(
        agent: Any,
        session_id: str,
        *,
        wait_seconds: float = 2.0,
    ) -> dict[str, Any]:
        session = module.load_session(session_id)
        status = str(session.get("status", ""))
        if status in _RETRIABLE_GENERATION_STATUSES:
            if not session.get("intent_consumed"):
                # A crash may leave status=generating before the intent is consumed.
                state = module._intent_auth_state(session)
                if state == "approved":
                    return generate(agent, session)
                if state == "pending":
                    return session
                session["status"] = "denied"
                session["error"] = "升级意图授权无效或已拒绝"
                module.save_session(session)
                return session
            return generate(agent, session)

        if status == "awaiting_candidate_approval":
            state = module._candidate_auth_state(session)
            if state == "denied":
                session["status"] = "candidate_denied"
                session["error"] = (
                    "主人拒绝了当前精确候选；原升级意图仍有效。"
                    "再次执行 /upgrade <session-id> 会在相同授权范围内生成新候选。"
                )
                session["next_action"] = f"/upgrade {session['id']}"
                module.save_session(session)
                return session
            if state == "invalid":
                return reissue_candidate_authorization(session)

        if status == "awaiting_intent_approval":
            state = module._intent_auth_state(session)
            if state == "invalid":
                module._request_intent_approval(session)
                session["next_action"] = f"/approve {session.get('intent_auth_id', '')}"
                module.save_session(session)
                return session

        return original_advance(agent, session_id, wait_seconds=wait_seconds)

    def public_status(session: dict[str, Any]) -> dict[str, Any]:
        value = original_public_status(session)
        value["generation_attempts"] = int(session.get("generation_attempts", 0) or 0)
        value["max_generation_attempts"] = int(
            session.get("max_generation_attempts", 0) or 0
        )
        if session.get("last_generation_error"):
            value["last_generation_error"] = str(session["last_generation_error"])
        candidate_files = _candidate_summary(session)
        if candidate_files:
            value["candidate_files"] = candidate_files
            binding = session.get("candidate_binding")
            if isinstance(binding, dict):
                value["candidate_binding_summary"] = {
                    "candidate_tree_sha256": str(
                        binding.get("candidate_tree_sha256", "")
                    ),
                    "test_report_sha256": str(binding.get("test_report_sha256", "")),
                    "baseline_manifest_sha256": str(
                        binding.get("baseline_manifest_sha256", "")
                    ),
                }
        if str(session.get("status", "")) in _RETRIABLE_GENERATION_STATUSES:
            value["next_action"] = f"/upgrade {session.get('id', '')}"
        return value

    module._build_prompt = build_prompt
    module._generate_candidate = generate
    module.advance_session = advance
    module.public_status = public_status
    module._agenelf_original_authorized_upgrade_generate = original_generate
    module._agenelf_original_authorized_upgrade_advance = original_advance
    module._agenelf_authorized_upgrade_recovery_installed = True
