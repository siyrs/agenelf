"""Diff-aware permanent redlines for owner-authorized upgrades.

Protected control-plane files legitimately contain approval-key paths, decision queues
and governance terms. Scanning the entire replacement file would therefore reject every
legitimate maintenance change. This module scans only newly introduced lines while also
requiring critical root-of-trust tokens to remain present.

The scanner is language-neutral for the production control plane: Python and
Node.js/TypeScript candidates pass through the same final diff-aware redline source.
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
            r"local/secrets|"
            r"(?:read_text|read_bytes|open)\([^\n]{0,180}(?:\.env|approval/key)|"
            r"(?:\.env|approval/key)[^\n]{0,180}(?:read_text|read_bytes|open)|"
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
    (
        "Node 任意 Shell",
        re.compile(
            r"(?:from\s+['\"]node:child_process['\"]|"
            r"require\(['\"](?:node:)?child_process['\"]\))"
            r"[\s\S]{0,1600}(?:\bexec(?:Sync)?\s*\(|"
            r"\bspawn(?:Sync)?\s*\([^\n]{0,360}\bshell\s*:\s*true)",
            re.I,
        ),
    ),
    (
        "Node 动态代码执行",
        re.compile(r"\b(?:eval|Function)\s*\(|\bvm\.(?:runIn|compileFunction)", re.I),
    ),
    (
        "关闭 TLS 校验",
        re.compile(r"NODE_TLS_REJECT_UNAUTHORIZED\s*[:=]\s*['\"]?0", re.I),
    ),
    (
        "npm 生命周期脚本",
        re.compile(
            r"['\"](?:preinstall|install|postinstall|prepublish|prepublishOnly|prepare)['\"]\s*:",
            re.I,
        ),
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
    "app/core/node_upgrade_policy.py": (
        "_NODE_SCOPES",
        "_prepare_changes",
        "_validate_node_syntax",
        "_FORBIDDEN_LIFECYCLE_SCRIPTS",
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
    "app/tests/test_node_candidate_contract.py": (
        ".agenelf-evolution-workspace.json",
        '"ci", "--ignore-scripts"',
        '"run", "test:node"',
    ),
    "Dockerfile.control-plane": (
        "FROM node:24.18.0-bookworm-slim AS node-runtime",
        "FROM python:3.12-slim",
        "npm_config_ignore_scripts=true",
        "USER agenelf",
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
            raise RuntimeError(
                f"候选 {normalized} 新增代码命中永久安全红线：{label}"
            )

    required = _REQUIRED_TOKENS_BY_PATH.get(normalized, ())
    missing = [token for token in required if token not in str(content or "")]
    if missing:
        raise RuntimeError(
            f"候选 {normalized} 删除了可信升级根约束：{', '.join(missing)}"
        )


def install(module: Any) -> None:
    """Install this scanner into the already imported authorized-upgrade module."""

    domain_error = getattr(module, "AuthorizedUpgradeError", None)
    if isinstance(domain_error, type) and issubclass(domain_error, Exception):
        def domain_scanner(path: str, content: str) -> None:
            try:
                scan_redlines(path, content)
            except RuntimeError as exc:
                raise domain_error(str(exc)) from exc

        module.scan_redlines = domain_scanner
    else:
        module.scan_redlines = scan_redlines
    module._agenelf_diff_redlines_installed = True
