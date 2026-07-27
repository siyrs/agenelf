"""Registry-level execution contracts and policy enforcement.

Risk answers how dangerous an operation is. ``execution_mode`` answers where and how
it may run.  Every external side effect is classified before skill execution and tool
arguments are never written to policy audit logs.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VALID_RISKS = {"read", "change", "privileged", "irreversible", "forbidden"}
VALID_EXECUTION_MODES = {
    "pure",
    "local_state",
    "queued_runner",
    "controlled_sandbox",
    "host_controlled",
    "forbidden",
}


@dataclass(frozen=True)
class ToolExecutionContract:
    tool: str
    capability: str
    operation: str
    risk: str
    execution_mode: str
    source: str = "explicit"

    def as_dict(self) -> dict[str, str]:
        return {
            "tool": self.tool,
            "capability": self.capability,
            "operation": self.operation,
            "risk": self.risk,
            "execution_mode": self.execution_mode,
            "source": self.source,
        }


def _contract(
    tool: str,
    capability: str,
    operation: str,
    risk: str,
    execution_mode: str,
) -> ToolExecutionContract:
    if risk not in VALID_RISKS:
        raise ValueError(f"未知风险：{risk}")
    if execution_mode not in VALID_EXECUTION_MODES:
        raise ValueError(f"未知 execution_mode：{execution_mode}")
    return ToolExecutionContract(tool, capability, operation, risk, execution_mode)


_EXPLICIT: dict[str, ToolExecutionContract] = {
    # Server operations.
    "list_managed_servers": _contract("list_managed_servers", "server.operations", "catalog", "read", "pure"),
    "get_server_operation": _contract("get_server_operation", "server.operations", "get_result", "read", "pure"),
    "inspect_server": _contract("inspect_server", "server.operations", "inspect", "read", "queued_runner"),
    "list_docker_containers": _contract("list_docker_containers", "server.operations", "docker_ps", "read", "queued_runner"),
    "update_apt_index": _contract("update_apt_index", "server.operations", "apt_update", "change", "queued_runner"),
    "install_docker": _contract("install_docker", "server.operations", "docker_install", "privileged", "queued_runner"),
    "deploy_compose_project": _contract("deploy_compose_project", "server.operations", "compose_deploy", "change", "queued_runner"),
    "down_compose_project": _contract("down_compose_project", "server.operations", "compose_down", "change", "queued_runner"),
    # Structured remote Docker operations.
    "list_docker_runtime": _contract("list_docker_runtime", "docker.operations", "catalog", "read", "pure"),
    "get_docker_operation": _contract("get_docker_operation", "docker.operations", "get_result", "read", "pure"),
    "get_docker_logs": _contract("get_docker_logs", "docker.operations", "get_docker_logs", "read", "queued_runner"),
    "inspect_docker_container": _contract("inspect_docker_container", "docker.operations", "inspect_docker_container", "read", "queued_runner"),
    "run_docker_check": _contract("run_docker_check", "docker.operations", "run_docker_check", "read", "queued_runner"),
    "restart_docker_container": _contract("restart_docker_container", "docker.operations", "restart_docker_container", "change", "queued_runner"),
    # Validation.
    "list_validation_checks": _contract("list_validation_checks", "software.validation", "catalog", "read", "pure"),
    "run_validation_check": _contract("run_validation_check", "software.validation", "run_check", "read", "queued_runner"),
    "run_validation_suite": _contract("run_validation_suite", "software.validation", "run_suite", "read", "queued_runner"),
    "get_validation_result": _contract("get_validation_result", "software.validation", "get_result", "read", "pure"),
    # Isolated code repair.
    "list_code_repair_repositories": _contract("list_code_repair_repositories", "code.repair", "catalog", "read", "pure"),
    "submit_code_repair_patch": _contract("submit_code_repair_patch", "code.repair", "submit_patch", "read", "queued_runner"),
    "get_code_repair_result": _contract("get_code_repair_result", "code.repair", "get_result", "read", "pure"),
    # Governed workflow state.
    "workflow_create_task": _contract("workflow_create_task", "agent.workflow", "create", "change", "local_state"),
    "workflow_list_tasks": _contract("workflow_list_tasks", "agent.workflow", "list", "read", "pure"),
    "workflow_get_task": _contract("workflow_get_task", "agent.workflow", "get", "read", "pure"),
    "workflow_transition_task": _contract("workflow_transition_task", "agent.workflow", "transition", "change", "local_state"),
    "workflow_update_step": _contract("workflow_update_step", "agent.workflow", "update_step", "change", "local_state"),
    "workflow_add_evidence": _contract("workflow_add_evidence", "agent.workflow", "add_evidence", "change", "local_state"),
    "workflow_next_action": _contract("workflow_next_action", "agent.workflow", "next_action", "read", "pure"),
    # Restart-safe continuation.
    "checkpoint_task_continuation": _contract("checkpoint_task_continuation", "agent.task_continuation", "checkpoint", "change", "local_state"),
    "task_continuation_status": _contract("task_continuation_status", "agent.task_continuation", "status", "read", "pure"),
    "complete_task_continuation": _contract("complete_task_continuation", "agent.task_continuation", "complete", "change", "local_state"),
    "retry_task_continuation": _contract("retry_task_continuation", "agent.task_continuation", "retry", "change", "local_state"),
    "cancel_task_continuation": _contract("cancel_task_continuation", "agent.task_continuation", "cancel", "change", "local_state"),
    # Owner context and memory.
    "get_local_context_status": _contract("get_local_context_status", "owner.context", "status", "read", "pure"),
    "reload_local_context": _contract("reload_local_context", "owner.context", "reload", "read", "local_state"),
    "remember_owner_context": _contract("remember_owner_context", "owner.context", "remember", "change", "local_state"),
    "recall_owner_context": _contract("recall_owner_context", "owner.context", "recall", "read", "pure"),
    # Persistent self-development.
    "self_development_status": _contract("self_development_status", "agent.self_development", "development_status", "read", "pure"),
    "reflect_and_sediment": _contract("reflect_and_sediment", "agent.self_development", "reflect_and_sediment", "change", "local_state"),
    "list_self_reflections": _contract("list_self_reflections", "agent.self_development", "list_reflections", "read", "pure"),
    "list_improvement_intentions": _contract("list_improvement_intentions", "agent.self_development", "list_intentions", "read", "pure"),
    "create_improvement_intention": _contract("create_improvement_intention", "agent.self_development", "create_intention", "change", "local_state"),
    "capability_health_snapshot": _contract("capability_health_snapshot", "agent.self_development", "capability_health", "read", "pure"),
    "improvement_roadmap": _contract("improvement_roadmap", "agent.self_development", "improvement_roadmap", "read", "pure"),
    # Bounded runtime tuning.
    "optimize_status": _contract("optimize_status", "agent.self_optimization", "optimize_status", "read", "pure"),
    "optimize_apply": _contract("optimize_apply", "agent.self_optimization", "optimize_apply", "change", "local_state"),
    "optimize_rollback": _contract("optimize_rollback", "agent.self_optimization", "optimize_rollback", "change", "local_state"),
    "optimize_auto": _contract("optimize_auto", "agent.self_optimization", "optimize_auto", "change", "local_state"),
    # Lightweight task board.
    "task_list": _contract("task_list", "agent.task_board", "task_list", "read", "pure"),
    "task_create": _contract("task_create", "agent.task_board", "task_create", "change", "local_state"),
    "task_advance": _contract("task_advance", "agent.task_board", "task_advance", "change", "local_state"),
    "task_complete": _contract("task_complete", "agent.task_board", "task_complete", "change", "local_state"),
    "task_block": _contract("task_block", "agent.task_board", "task_block", "change", "local_state"),
    "task_link_intention": _contract("task_link_intention", "agent.task_board", "task_link_intention", "change", "local_state"),
    "create_todo": _contract("create_todo", "task_handler", "create_todo", "change", "local_state"),
    "save_note": _contract("save_note", "task_handler", "save_note", "change", "local_state"),
    "read_note": _contract("read_note", "task_handler", "read_note", "read", "pure"),
    # Scratch and skill forge.
    "write_code_file": _contract("write_code_file", "code.scratch", "write_code_file", "change", "local_state"),
    "run_python": _contract("run_python", "code.scratch", "run_python", "forbidden", "forbidden"),
    "forge_skill": _contract("forge_skill", "agent.skill_forge", "forge_skill", "change", "host_controlled"),
    "list_forged_skills": _contract("list_forged_skills", "agent.skill_forge", "list_forged_skills", "read", "pure"),
    "remove_forged_skill": _contract("remove_forged_skill", "agent.skill_forge", "remove_forged_skill", "change", "host_controlled"),
    # Controlled self-evolution sandbox.
    "evolution_begin": _contract("evolution_begin", "agent.evolution", "begin", "change", "controlled_sandbox"),
    "evolution_write_file": _contract("evolution_write_file", "agent.evolution", "write_file", "change", "controlled_sandbox"),
    "evolution_run_tests": _contract("evolution_run_tests", "agent.evolution", "run_tests", "change", "controlled_sandbox"),
    "evolution_request_promotion": _contract("evolution_request_promotion", "agent.evolution", "request_promotion", "change", "controlled_sandbox"),
    "evolution_status": _contract("evolution_status", "agent.evolution", "status", "read", "pure"),
}

_LEGACY_PURE_TOOLS = {"ask_llm", "growth_pulse", "summarize"}


def resolve_contract(tool_name: str, args: dict[str, Any], module: Any) -> ToolExecutionContract | None:
    tool_name = str(tool_name or "").strip()
    data = args if isinstance(args, dict) else {}

    if tool_name == "manage_system_service":
        action = str(data.get("action", "")).strip().lower()
        if action == "status":
            return _contract(tool_name, "server.operations", "service_status", "read", "queued_runner")
        if action == "restart":
            return _contract(tool_name, "server.operations", "service_restart", "change", "queued_runner")
        if not action:
            return ToolExecutionContract(
                tool_name,
                "server.operations",
                "dynamic:action",
                "read",
                "queued_runner",
                source="dynamic-placeholder",
            )
        return None

    if tool_name == "pursue_improvement_intention":
        if bool(data.get("apply_changes", False)):
            return _contract(
                tool_name,
                "agent.self_development",
                "pursue_intention",
                "change",
                "controlled_sandbox",
            )
        return _contract(
            tool_name,
            "agent.self_development",
            "pursue_intention",
            "change",
            "local_state",
        )

    explicit = _EXPLICIT.get(tool_name)
    if explicit is not None:
        return explicit

    raw = getattr(module, "CAPABILITY_META", None)
    if isinstance(raw, dict):
        capability = str(
            raw.get("id") or getattr(module, "SKILL_META", {}).get("name") or ""
        ).strip()
        operations = raw.get("operations", [])
        declared = [item for item in operations if isinstance(item, dict)] if isinstance(operations, list) else []
        for item in declared:
            if str(item.get("name", "")).strip() != tool_name:
                continue
            risk = str(item.get("risk", "read")).strip().lower()
            mode = str(item.get("execution_mode", "")).strip()
            if mode not in VALID_EXECUTION_MODES:
                mode = "pure" if risk == "read" else "local_state" if risk == "change" else "queued_runner"
                if risk == "forbidden":
                    mode = "forbidden"
            return ToolExecutionContract(
                tool_name,
                capability,
                tool_name,
                risk,
                mode,
                source="exact-operation",
            )
        if declared and all(
            str(item.get("risk", "read")).strip().lower() == "read" for item in declared
        ):
            return ToolExecutionContract(
                tool_name,
                capability,
                tool_name,
                "read",
                "pure",
                source="read-only-capability",
            )

    if tool_name in _LEGACY_PURE_TOOLS:
        skill_meta = getattr(module, "SKILL_META", {})
        capability = str(skill_meta.get("name") or module.__name__).strip()
        return ToolExecutionContract(
            tool_name,
            capability,
            tool_name,
            "read",
            "pure",
            source="legacy-pure-allowlist",
        )
    return None


def evaluate_contract(
    policy_engine: Any | None,
    contract: ToolExecutionContract | None,
    subject: str,
) -> dict[str, Any]:
    subject = str(subject or "agent").strip()
    if contract is None:
        if policy_engine is None:
            return {
                "allowed": True,
                "reason": "测试/兼容运行时未绑定 PolicyEngine",
                "policy_version": "unbound",
            }
        return {
            "allowed": False,
            "reason": "工具缺少 execution contract，默认拒绝",
            "policy_version": getattr(policy_engine, "policy_version", "unknown"),
        }
    if contract.execution_mode == "forbidden" or contract.risk == "forbidden":
        return {
            "allowed": False,
            "reason": "execution_mode=forbidden，任何渠道都不可执行",
            "policy_version": getattr(policy_engine, "policy_version", "unknown"),
        }
    if policy_engine is None:
        return {
            "allowed": True,
            "reason": "测试/兼容运行时未绑定 PolicyEngine",
            "policy_version": "unbound",
        }

    decision = policy_engine.evaluate_declared(
        contract.capability,
        contract.operation,
        contract.risk,
        subject=subject,
    )
    result = dict(decision) if isinstance(decision, dict) else {
        "allowed": False,
        "reason": "策略引擎返回异常",
    }
    if contract.execution_mode == "pure" and contract.risk != "read":
        result.update(allowed=False, reason="pure 模式只能声明 read 风险")
    elif contract.execution_mode == "local_state":
        if contract.risk not in {"read", "change"}:
            result.update(allowed=False, reason="local_state 不允许 privileged/irreversible 风险")
        elif result.get("allowed"):
            result.update(auto_execute=True, approval="none")
            result["reason"] = str(result.get("reason", "")) + "；仅修改 Agenelf 有界本地状态，不触发外部系统"
    elif contract.execution_mode == "controlled_sandbox":
        if subject in {"mobile_device", "voice"}:
            result.update(allowed=False, reason="移动端/语音不能直接触发受控代码沙盒")
        elif result.get("allowed"):
            result["reason"] = str(result.get("reason", "")) + "；仅允许 app-tmp 测试与晋升申请，不能直接修改 main"
    elif contract.execution_mode == "host_controlled":
        if subject not in {"cli", "host"}:
            result.update(allowed=False, reason="host_controlled 工具只允许宿主机或显式 CLI 操作")
    elif contract.execution_mode == "queued_runner" and result.get("allowed"):
        result["reason"] = str(result.get("reason", "")) + "；实际副作用必须由指纹绑定的确定性 Runner 执行"
    result.setdefault("policy_version", getattr(policy_engine, "policy_version", "unknown"))
    return result


def audit_dispatch(
    tool_name: str,
    contract: ToolExecutionContract | None,
    subject: str,
    decision: dict[str, Any],
) -> None:
    configured = os.environ.get("AGENELF_ROOT", "").strip()
    root = Path(configured).resolve() if configured else Path(__file__).resolve().parents[2]
    path = root / "logs" / "policy-dispatch.jsonl"
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool": str(tool_name),
        "subject": str(subject),
        "allowed": bool(decision.get("allowed", False)),
        "reason": str(decision.get("reason", ""))[:1000],
        "policy_version": str(decision.get("policy_version", "")),
        "contract": contract.as_dict() if contract else None,
        "arguments_logged": False,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass
