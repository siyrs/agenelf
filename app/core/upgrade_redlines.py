"""Diff-aware permanent redlines for owner-authorized upgrades.

Protected control-plane files legitimately contain approval-key paths, decision queues
and governance terms. Scanning the entire replacement file would therefore reject every
legitimate maintenance change. This module scans only newly introduced lines while also
requiring critical root-of-trust tokens to remain present.

It is installed into the trusted ``core.authorized_upgrade`` module in both the Agent
candidate stage and the isolated application runner. The baseline root is the staged
candidate before edits for the Agent and the live explicit upgrade target for the
runner.
"""
from __future__ import annotations

import difflib
import os
import re
from pathlib import Path
from typing import Any

_REDLINE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Docker Socket", re.compile(r"/var/run/docker\.sock|docker\.sock", re.I)),
    (
        "新增凭据读取",
        re.compile(
            r"local/secrets|(?:read_text|read_bytes|open)\([^\n]{0,180}(?:\.env|approval/key)|"
            r"AGENELF_APPROVAL_KEY[^\n]{0,180}(?:read|open)|"
            r"auth-decisions[^\n]{0,180}(?:write_text|write_bytes|open\([^)]*['\"]w)",
            re.I,
        ),
    ),
    (
        "自我批准",
        re.compile(
            r"self[_ -]?approve|自动批准|伪造授权|forge[_ -]?owner|"
            r"decision\s*=\s*['\"]approve['\"]",
            re.I,
        ),
    ),
    (
        "审计破坏",
        re.compile(
            r"(?:unlink|remove|rmtree|truncate)[^\n]{0,160}"
            r"(?:audit|auth-decisions|promotion-history|self-upgrade-results)",
            re.I,
        ),
    ),
    (
        "测试或门禁绕过",
        re.compile(
            r"monkey.?patch[^\n]{0,160}(?:test|gate|policy)|"
            r"disable[^\n]{0,100}(?:test|gate|audit|policy)|"
            r"skip[^\n]{0,100}(?:governance|security|existing[_ -]?test)",
            re.I,
        ),
    ),
    (
        "危险远程脚本",
        re.compile(r"(?:curl|wget)[^\n|]{0,240}\|\s*(?:sudo\s+)?(?:ba)?sh\b", re.I),
    ),
    (
        "直接主分支发布",
        re.compile(r"git[^\n]{0,160}(?:push|merge)[^\n]{0,160}\bmain\b", re.I),
    ),
    ("明显 API Key", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
)

_REQUIRED_TOKENS_BY_PATH: dict[str, tuple[str, ...]] = {
    "policy/safety-constraints.v1.yaml": (
        "owner_authorized_upgrade:",
        "owner_authorization_cannot_be_generated_by_model_output",
        "no_self_approval_or_forged_owner_decision",
        "no_access_to_env_local_secrets_ssh_keys_or_approval_key",
        "no_test_gate_policy_or_audit_weakening_to_force_success",
        "no_direct_push_or_merge_main_from_autonomous_runtime",
    ),
    "scripts/validate_governance.py": (
        "REQUIRED_UPGRADE_REDLINE",
        "validate_owner_authorized_upgrade",
        "authorized_upgrade_runner_isolated",
        "backup_and_rollback_evidence_archived",
    ),
    "scripts/self_upgrade_runner.py": (
        "verify_candidate",
        "rerun_tests",
        "backup_targets",
        "rollback",
        "consume_auth",
    ),
    "scripts/run_authorized_upgrade_tests.py": (
        "verify_existing_tests",
        "validate_governance.py",
        "unittest",
    ),
    "app/core/authorized_upgrade.py": (
        "_PERMANENTLY_FORBIDDEN_PREFIXES",
        "_request_candidate_approval",
        "candidate_tree_sha256",
        "scan_redlines",
    ),
    "app/core/cli_approval.py": (
        "parse_owner_decision",
        "submit_owner_command",
        "_advance_upgrade_after_approval",
    ),
    "app/skills/authorized_self_upgrade.py": (
        "request_authorized_self_upgrade",
        "continue_authorized_self_upgrade",
        "_ORDINARY_SANDBOX_PROTECTED",
    ),
}


def _runtime_root() -> Path:
    configured = os.environ.get("AGENELF_ROOT", "").strip()
    return Path(configured).resolve() if configured else Path(__file__).resolve().parents[2]


def _baseline_root() -> Path:
    configured = os.environ.get("AGENELF_REDLINE_BASE_ROOT", "").strip()
    if configured:
        return Path(configured).resolve()
    return (_runtime_root() / "app-tmp" / "repo").resolve()


def _baseline_content(path: str) -> str:
    candidate = (_baseline_root() / path).resolve()
    root = _baseline_root()
    if not candidate.is_relative_to(root) or not candidate.is_file() or candidate.is_symlink():
        return ""
    try:
        return candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def added_text(before: str, after: str) -> str:
    lines: list[str] = []
    for line in difflib.unified_diff(
        before.splitlines(),
        after.splitlines(),
        lineterm="",
    ):
        if line.startswith("+") and not line.startswith("+++"):
            lines.append(line[1:])
    return "\n".join(lines)


def scan_redlines(path: str, content: str) -> None:
    """Reject newly introduced redlines and removal of root-of-trust invariants."""

    normalized = str(path or "").replace("\\", "/").lstrip("./")
    before = _baseline_content(normalized)
    additions = added_text(before, str(content or ""))
    for label, pattern in _REDLINE_PATTERNS:
        if pattern.search(additions):
            raise RuntimeError(f"候选 {normalized} 新增代码命中永久安全红线：{label}")

    required = _REQUIRED_TOKENS_BY_PATH.get(normalized, ())
    missing = [token for token in required if token not in str(content or "")]
    if missing:
        raise RuntimeError(
            f"候选 {normalized} 删除了可信升级根约束：{', '.join(missing)}"
        )


def install(module: Any) -> None:
    """Install this scanner into the already imported authorized-upgrade module."""

    module.scan_redlines = scan_redlines
    module._agenelf_diff_redlines_installed = True
