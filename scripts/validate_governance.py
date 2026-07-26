#!/usr/bin/env python3
"""Validate Agenelf's machine-readable governance baseline.

The validator is dependency-light, deterministic and intended for local use and CI.
It rejects policies that omit mandatory risk levels, weaken forbidden behavior,
allow autonomous main-branch promotion, fail to bind owner authorization to an exact
operation payload, or weaken the two-stage owner-authorized self-upgrade contract.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

REQUIRED_RISK_LEVELS = {"read", "change", "privileged", "irreversible", "forbidden"}
REQUIRED_BINDING_FIELDS = {
    "capability",
    "operation",
    "target",
    "canonical_parameters_hash",
    "risk",
    "nonce",
    "issued_at",
    "expires_at",
}
REQUIRED_FORBIDDEN = {
    "self_approve_or_forge_owner_decision",
    "modify_or_delete_audit_evidence",
    "disable_or_bypass_policy_engine",
    "expose_secrets_to_llm_or_chat_history",
    "execute_model_generated_arbitrary_shell",
    "execute_model_generated_code_in_agent_process",
    "push_or_merge_main_directly_from_autonomous_runtime",
    "weaken_tests_or_gate_to_make_a_candidate_pass",
}
REQUIRED_PROTECTED_PREFIXES = {
    "policy/",
    "scripts/",
    ".github/workflows/",
    "local/secrets/",
    "local/repositories.yaml",
    "app/core/registry.py",
    "app/core/code_repair.py",
    "app/core/permissions.py",
    "app/core/operations.py",
    "app/core/autonomy.py",
    "app/core/authorized_upgrade.py",
    "app/core/approval_catalog.py",
    "app/skills/authorized_self_upgrade.py",
}
REQUIRED_GATES = {
    "policy_schema_valid",
    "full_unit_suite_passed",
    "exact_authorization_binding_verified",
    "trusted_evidence_archived",
    "documentation_updated",
    "authorized_upgrade_intent_binding_verified",
    "authorized_upgrade_candidate_binding_verified",
    "authorized_upgrade_runner_isolated",
    "backup_and_rollback_evidence_archived",
}
REQUIRED_UPGRADE_REDLINE = {
    "owner_authorization_cannot_be_generated_by_model_output",
    "no_self_approval_or_forged_owner_decision",
    "no_access_to_env_local_secrets_ssh_keys_or_approval_key",
    "no_write_to_auth_decisions_audit_evidence_or_git_metadata",
    "no_test_gate_policy_or_audit_weakening_to_force_success",
    "no_docker_socket_or_model_generated_arbitrary_shell",
    "no_direct_push_or_merge_main_from_autonomous_runtime",
}
REQUIRED_UPGRADE_SCOPES = {
    "app_runtime",
    "skills",
    "tests",
    "runners",
    "policy",
    "compose",
    "ci",
    "docs",
    "authorization_control",
}


def _mapping(value: Any, path: str, errors: list[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        errors.append(f"{path} 必须是对象")
        return {}
    return value


def _string_set(value: Any, path: str, errors: list[str]) -> set[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        errors.append(f"{path} 必须是字符串数组")
        return set()
    return {item.strip() for item in value if item.strip()}


def _bool_true(value: Any, path: str, errors: list[str]) -> None:
    if value is not True:
        errors.append(f"{path} 必须为 true")


def validate_owner_authorized_upgrade(root: dict[str, Any], errors: list[str]) -> None:
    upgrade = _mapping(
        root.get("owner_authorized_upgrade"),
        "owner_authorized_upgrade",
        errors,
    )
    _bool_true(upgrade.get("enabled"), "owner_authorized_upgrade.enabled", errors)
    _bool_true(
        upgrade.get("default_for_protected_paths"),
        "owner_authorized_upgrade.default_for_protected_paths",
        errors,
    )

    stages = upgrade.get("stages")
    if not isinstance(stages, list):
        errors.append("owner_authorized_upgrade.stages 必须是数组")
        stage_map: dict[str, dict[str, Any]] = {}
    else:
        stage_map = {
            str(item.get("name", "")): item
            for item in stages
            if isinstance(item, dict) and item.get("name")
        }
    for name in ("intent_scope_approval", "tested_candidate_approval"):
        stage = _mapping(
            stage_map.get(name),
            f"owner_authorized_upgrade.stages.{name}",
            errors,
        )
        _bool_true(stage.get("single_use"), f"upgrade stage {name}.single_use", errors)
        _bool_true(stage.get("expires"), f"upgrade stage {name}.expires", errors)
        binds = _string_set(stage.get("binds"), f"upgrade stage {name}.binds", errors)
        if name == "intent_scope_approval":
            required = {
                "goal_sha256",
                "scopes",
                "allowed_paths",
                "max_files",
                "max_changed_lines",
                "redline_policy",
            }
        else:
            required = {
                "session_id",
                "intent_auth_id",
                "changed_file_hashes",
                "candidate_tree_sha256",
                "test_report_sha256",
                "baseline_manifest_sha256",
            }
        missing = required - binds
        if missing:
            errors.append(
                f"升级阶段 {name} 绑定字段缺失：{', '.join(sorted(missing))}"
            )

    execution = _mapping(
        upgrade.get("execution"),
        "owner_authorized_upgrade.execution",
        errors,
    )
    if execution.get("candidate_workspace") != "app-tmp/repo":
        errors.append("授权升级候选必须位于 app-tmp/repo")
    if execution.get("runner") != "self-upgrade-runner":
        errors.append("授权升级必须由 self-upgrade-runner 应用")
    if execution.get("runner_network") != "none":
        errors.append("self-upgrade-runner 必须无网络")
    for key in (
        "backup_before_apply",
        "rollback_on_partial_failure",
        "stale_target_hash_rejected",
        "skill_hot_reload_when_safe",
        "restart_checkpoint_for_core_or_runner_changes",
    ):
        _bool_true(execution.get(key), f"owner_authorized_upgrade.execution.{key}", errors)

    scopes = _string_set(
        upgrade.get("allowed_scopes"),
        "owner_authorized_upgrade.allowed_scopes",
        errors,
    )
    missing_scopes = REQUIRED_UPGRADE_SCOPES - scopes
    if missing_scopes:
        errors.append(
            "授权升级范围缺失：" + ", ".join(sorted(missing_scopes))
        )

    redlines = _string_set(
        upgrade.get("permanent_redlines"),
        "owner_authorized_upgrade.permanent_redlines",
        errors,
    )
    missing_redlines = REQUIRED_UPGRADE_REDLINE - redlines
    if missing_redlines:
        errors.append(
            "授权升级永久红线缺失：" + ", ".join(sorted(missing_redlines))
        )

    tests = _mapping(
        upgrade.get("tests"),
        "owner_authorized_upgrade.tests",
        errors,
    )
    for key in (
        "existing_tests_immutable",
        "new_regression_test_required_for_code_change",
        "complete_suite_required",
        "candidate_revalidated_by_runner",
    ):
        _bool_true(tests.get(key), f"owner_authorized_upgrade.tests.{key}", errors)


def validate_policy(policy: Any) -> list[str]:
    errors: list[str] = []
    root = _mapping(policy, "root", errors)
    if root.get("schema_version") != 1:
        errors.append("schema_version 必须为 1")
    if root.get("status") != "active":
        errors.append("status 必须为 active")

    metadata = _mapping(root.get("metadata"), "metadata", errors)
    if metadata.get("consciousness_claim") is not False:
        errors.append("metadata.consciousness_claim 必须显式为 false")

    principles = root.get("principles")
    if not isinstance(principles, list) or len(principles) < 5:
        errors.append("principles 至少需要 5 条")

    risk_levels = _mapping(root.get("risk_levels"), "risk_levels", errors)
    missing_risks = REQUIRED_RISK_LEVELS - set(risk_levels)
    if missing_risks:
        errors.append(f"缺少风险级别：{', '.join(sorted(missing_risks))}")
    for risk in ("change", "privileged", "irreversible", "forbidden"):
        cfg = _mapping(risk_levels.get(risk), f"risk_levels.{risk}", errors)
        if cfg.get("auto_execute") is not False:
            errors.append(f"risk_levels.{risk}.auto_execute 必须为 false")
    if _mapping(risk_levels.get("forbidden"), "risk_levels.forbidden", errors).get(
        "approval"
    ) != "impossible":
        errors.append("forbidden 风险必须不可授权")

    authorization = _mapping(
        root.get("owner_authorization"),
        "owner_authorization",
        errors,
    )
    if authorization.get("never_overrides_forbidden") is not True:
        errors.append("owner_authorization.never_overrides_forbidden 必须为 true")
    fields = _string_set(
        authorization.get("exact_binding_fields"),
        "owner_authorization.exact_binding_fields",
        errors,
    )
    missing_fields = REQUIRED_BINDING_FIELDS - fields
    if missing_fields:
        errors.append(f"授权绑定字段缺失：{', '.join(sorted(missing_fields))}")
    modes = _mapping(
        authorization.get("modes"),
        "owner_authorization.modes",
        errors,
    )
    for mode in ("owner_exact", "owner_elevated", "owner_irreversible"):
        if mode not in modes:
            errors.append(f"缺少授权模式：{mode}")

    forbidden = _string_set(
        root.get("forbidden_behaviors"),
        "forbidden_behaviors",
        errors,
    )
    missing_forbidden = REQUIRED_FORBIDDEN - forbidden
    if missing_forbidden:
        errors.append(f"永久禁止行为缺失：{', '.join(sorted(missing_forbidden))}")

    protected = _string_set(root.get("protected_paths"), "protected_paths", errors)
    missing_paths = REQUIRED_PROTECTED_PREFIXES - protected
    if missing_paths:
        errors.append(f"受保护路径缺失：{', '.join(sorted(missing_paths))}")

    validate_owner_authorized_upgrade(root, errors)

    evolution = _mapping(root.get("self_evolution"), "self_evolution", errors)
    if evolution.get("auto_pursue") is not False:
        errors.append("self_evolution.auto_pursue 必须为 false")
    if evolution.get("protected_mode") != "owner_authorized_two_stage":
        errors.append("受保护自进化必须使用 owner_authorized_two_stage")
    forbidden_evolution = _string_set(
        evolution.get("forbidden"),
        "self_evolution.forbidden",
        errors,
    )
    for behavior in (
        "autonomously_merge_main",
        "modify_owner_decisions",
        "modify_or_read_secrets",
        "modify_delete_or_monkey_patch_existing_tests",
    ):
        if behavior not in forbidden_evolution:
            errors.append(f"自进化永久禁止行为缺失：{behavior}")
    limits = _mapping(
        evolution.get("candidate_limits"),
        "self_evolution.candidate_limits",
        errors,
    )
    if limits.get("tests_required") is not True or limits.get("full_suite_required") is not True:
        errors.append("自主候选必须要求测试和完整测试套件")
    if limits.get("immutable_digest_required") is not True:
        errors.append("自主候选必须绑定不可变摘要")
    if limits.get("max_files") != 10 or limits.get("max_changed_lines") != 500:
        errors.append("普通沙盒兼容上限必须保持 max_files=10、max_changed_lines=500")
    if not isinstance(limits.get("authorized_hard_max_files"), int) or int(
        limits.get("authorized_hard_max_files", 0)
    ) > 20:
        errors.append("授权升级文件硬上限不得超过 20")
    if not isinstance(limits.get("authorized_hard_max_changed_lines"), int) or int(
        limits.get("authorized_hard_max_changed_lines", 0)
    ) > 4000:
        errors.append("授权升级变更行硬上限不得超过 4000")

    model = _mapping(root.get("model_governance"), "model_governance", errors)
    if model.get("model_is_untrusted_planner") is not True:
        errors.append("模型必须被视为不可信规划器")
    if model.get("secrets_in_prompt") is not False:
        errors.append("不得允许凭据进入模型提示词")
    if model.get("model_output_never_counts_as_owner_authorization") is not True:
        errors.append("模型输出不得被视为主人授权")

    gates = _string_set(root.get("acceptance_gates"), "acceptance_gates", errors)
    missing_gates = REQUIRED_GATES - gates
    if missing_gates:
        errors.append(f"验收门缺失：{', '.join(sorted(missing_gates))}")
    return errors


def validate_execution_modes(policy: Any) -> list[str]:
    errors: list[str] = []
    root = _mapping(policy, "execution_modes_root", errors)
    if root.get("schema_version") != 1 or root.get("status") != "active":
        errors.append("execution-modes 策略必须 schema_version=1 且 status=active")
    defaults = _mapping(root.get("defaults"), "execution_modes.defaults", errors)
    if defaults.get("unclassified_tool") != "deny":
        errors.append("未分类工具必须默认拒绝")
    if defaults.get("arguments_in_audit") is not False:
        errors.append("执行审计不得记录工具参数")
    modes = _mapping(root.get("execution_modes"), "execution_modes", errors)
    required = {
        "pure",
        "local_state",
        "queued_runner",
        "controlled_sandbox",
        "host_controlled",
        "forbidden",
    }
    missing = required - set(modes)
    if missing:
        errors.append(f"execution_mode 缺失：{', '.join(sorted(missing))}")
    forbidden = _mapping(modes.get("forbidden"), "execution_modes.forbidden", errors)
    if forbidden.get("approval") != "impossible":
        errors.append("forbidden execution_mode 必须不可授权")
    return errors


def load_policy(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 Agenelf 治理策略")
    parser.add_argument(
        "path",
        nargs="?",
        default="policy/safety-constraints.v1.yaml",
        help="策略 YAML 路径",
    )
    args = parser.parse_args()
    path = Path(args.path)
    try:
        policy = load_policy(path)
    except (OSError, yaml.YAMLError) as exc:
        print(f"治理策略读取失败：{exc}", file=sys.stderr)
        return 2
    errors = validate_policy(policy)
    execution_path = path.parent / "execution-modes.v1.yaml"
    try:
        execution_policy = load_policy(execution_path)
    except (OSError, yaml.YAMLError) as exc:
        print(f"执行模式策略读取失败：{exc}", file=sys.stderr)
        return 2
    errors.extend(validate_execution_modes(execution_policy))
    if errors:
        print("治理策略校验失败：", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"治理策略校验通过：{path} + {execution_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
