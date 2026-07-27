"""Owner-authorized self-upgrade capability.

Protected runtime and control-plane code remains writable only in ``app-tmp`` until the
owner approves both the intent and the exact tested candidate. A deterministic runner
then applies the approved file manifest and returns evidence for hot reload or restart.
"""
from __future__ import annotations

import json
from typing import Any

from core import authorized_upgrade

SKILL_META = {
    "name": "authorized_self_upgrade",
    "description": (
        "主人两阶段授权的自我升级：先绑定目标/范围，再绑定测试通过的精确候选；"
        "支持受保护运行时、Runner、策略、Compose 和 CI 文件，但永久安全红线不可授权。"
    ),
    "version": "1.0.0",
}

CAPABILITY_META = {
    "id": "agent.authorized_self_upgrade",
    "name": "主人授权自我升级",
    "description": (
        "让 Agenelf 在主人批准的精确路径范围内生成、测试并应用代码升级；"
        "候选摘要需要第二次批准，应用由无网络隔离 Runner 完成。"
    ),
    "version": "1.0.0",
    "domain": "agent-governance",
    "operations": [
        {
            "name": "request_authorized_self_upgrade",
            "description": "创建绑定目标、范围和规模上限的升级意图授权",
            "risk": "change",
        },
        {
            "name": "continue_authorized_self_upgrade",
            "description": "在授权后生成候选、运行测试、申请候选批准并排队应用",
            "risk": "change",
        },
        {
            "name": "authorized_self_upgrade_status",
            "description": "查看升级授权、候选、应用和重载状态",
            "risk": "read",
        },
        {
            "name": "list_authorized_upgrade_scopes",
            "description": "查看可授权升级范围与永久红线",
            "risk": "read",
        },
    ],
    "composes_with": [
        "agent.evolution",
        "agent.self_development",
        "agent.task_continuation",
        "software.validation",
    ],
}

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "request_authorized_self_upgrade",
            "description": (
                "当升级需要修改受保护运行时、Runner、策略、Compose、CI 或审批控制面时，"
                "创建主人意图授权。返回 auth-...；主人在 CLI 用 /approve 精确批准。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string", "description": "具体升级目标与验收结果"},
                    "scopes": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "app_runtime",
                                "skills",
                                "tests",
                                "runners",
                                "policy",
                                "compose",
                                "ci",
                                "docs",
                                "authorization_control",
                            ],
                        },
                        "description": "可选；省略时按目标确定性分类",
                    },
                    "max_files": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                    },
                    "max_changed_lines": {
                        "type": "integer",
                        "minimum": 50,
                        "maximum": 4000,
                    },
                },
                "required": ["goal"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "continue_authorized_self_upgrade",
            "description": (
                "继续指定升级会话。意图批准后生成并测试候选；候选批准后提交隔离应用，"
                "技能文件会尽量热重载，核心/Runner/Compose 变更会保存重启续跑检查点。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "upgrade-... 会话 ID",
                    },
                    "wait_seconds": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 30,
                    },
                },
                "required": ["session_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "authorized_self_upgrade_status",
            "description": (
                "查看指定升级会话；省略 session_id 时返回最近一次会话。"
                "如果隔离应用已经完成，会顺便核对并收敛状态。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"session_id": {"type": "string"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_authorized_upgrade_scopes",
            "description": "列出可申请的升级范围和不可被主人授权覆盖的永久红线。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
]

_RUNTIME_AGENT: Any | None = None
_RUNTIME_CONFIG: dict[str, Any] = {}

# The ordinary sandbox must not be able to rewrite the mechanism that decides whether
# a protected change needs two-stage owner approval. The dedicated authorized runner
# can still update these files after exact approval.
_ORDINARY_SANDBOX_PROTECTED = {
    "core/authorized_upgrade.py",
    "core/approval_catalog.py",
    "core/owner_approval.py",
    "core/cli_approval.py",
    "core/execution_policy.py",
    "skills/authorized_self_upgrade.py",
    "skills/evolution_scope_guard.py",
}


def _install_ordinary_sandbox_guard() -> None:
    try:
        from core import autonomy

        current = set(getattr(autonomy, "_PROTECTED_PATHS", frozenset()))
        autonomy._PROTECTED_PATHS = frozenset(current | _ORDINARY_SANDBOX_PROTECTED)
    except Exception:
        # The host gate and policy validator independently protect the same files; a
        # runtime import issue must not prevent the Agent from starting.
        return


def configure_runtime(
    *,
    agent: Any,
    config: dict[str, Any] | None = None,
    **_: Any,
) -> None:
    global _RUNTIME_AGENT, _RUNTIME_CONFIG
    _RUNTIME_AGENT = agent
    _RUNTIME_CONFIG = config if isinstance(config, dict) else getattr(agent, "config", {})
    _install_ordinary_sandbox_guard()


def _agent() -> Any:
    if _RUNTIME_AGENT is None:
        raise RuntimeError("authorized_self_upgrade 尚未绑定 Agent 运行时")
    return _RUNTIME_AGENT


def _upgrade_config() -> dict[str, Any]:
    autonomy = _RUNTIME_CONFIG.get("autonomy", {}) if isinstance(_RUNTIME_CONFIG, dict) else {}
    if not isinstance(autonomy, dict):
        return {}
    value = autonomy.get("owner_authorized_upgrade", {})
    return value if isinstance(value, dict) else {}


def _bounded_default(name: str, fallback: int, minimum: int, maximum: int) -> int:
    try:
        value = int(_upgrade_config().get(name, fallback))
    except (TypeError, ValueError):
        value = fallback
    return max(minimum, min(value, maximum))


def request_authorized_self_upgrade(
    goal: str,
    scopes: list[str] | None = None,
    max_files: int | None = None,
    max_changed_lines: int | None = None,
) -> dict[str, Any]:
    files = (
        _bounded_default("default_max_files", authorized_upgrade.DEFAULT_MAX_FILES, 1, 20)
        if max_files is None
        else max(1, min(int(max_files), 20))
    )
    lines = (
        _bounded_default(
            "default_max_changed_lines",
            authorized_upgrade.DEFAULT_MAX_CHANGED_LINES,
            50,
            4000,
        )
        if max_changed_lines is None
        else max(50, min(int(max_changed_lines), 4000))
    )
    session = authorized_upgrade.create_or_get_session(
        goal,
        scopes,
        max_files=files,
        max_changed_lines=lines,
    )
    return authorized_upgrade.public_status(session)


def continue_authorized_self_upgrade(
    session_id: str,
    wait_seconds: float = 2.0,
) -> dict[str, Any]:
    session = authorized_upgrade.advance_session(
        _agent(),
        session_id,
        wait_seconds=max(0.0, min(float(wait_seconds), 30.0)),
    )
    return authorized_upgrade.public_status(session)


def _reconcile_session(session: dict[str, Any]) -> dict[str, Any]:
    if session.get("status") == "apply_queued" and _RUNTIME_AGENT is not None:
        return authorized_upgrade.advance_session(
            _RUNTIME_AGENT,
            str(session["id"]),
            wait_seconds=0,
        )
    return session


def authorized_self_upgrade_status(session_id: str = "") -> dict[str, Any]:
    if str(session_id or "").strip():
        session = authorized_upgrade.load_session(str(session_id).strip())
        return authorized_upgrade.public_status(_reconcile_session(session))
    sessions = authorized_upgrade.list_sessions(limit=1)
    if not sessions:
        return {"exists": False, "status": "none"}
    return authorized_upgrade.public_status(_reconcile_session(sessions[0]))


def list_authorized_upgrade_scopes() -> dict[str, Any]:
    return {
        "scopes": {
            "app_runtime": "app/core 下的核心运行时代码",
            "skills": "app/skills 下的技能实现",
            "tests": "仅允许新增 app/tests/test_*.py，既有测试不可修改",
            "runners": "scripts 下的确定性 Runner 与宿主机脚本",
            "policy": "policy 下的治理规则",
            "compose": "Compose、Dockerfile 与公开环境变量模板",
            "ci": "GitHub Actions 与供应链配置",
            "docs": "docs、README 与 Makefile",
            "authorization_control": "审批与授权控制面；仍受两阶段批准和永久红线约束",
        },
        "permanent_redlines": [
            "不得访问或修改 .env、local/、secrets/、data/、授权决定、审计记录或 Git 元数据",
            "不得自我批准、伪造主人决定或让模型输出成为授权",
            "不得削弱测试、门禁、策略或审计来使候选通过",
            "不得挂载 Docker Socket、执行模型生成任意 Shell、泄露凭据",
            "不得从自主运行时直接 push/merge main",
        ],
        "approval_stages": [
            "升级意图：目标、范围、允许路径、文件数和变更行数",
            "精确候选：变更文件前后摘要、候选树摘要和测试报告摘要",
        ],
    }


def route_goal(
    agent: Any,
    goal: str,
    scope_hints: list[str] | None = None,
) -> dict[str, Any]:
    """Used by the scope guard to create/advance one protected upgrade goal."""

    global _RUNTIME_AGENT
    _RUNTIME_AGENT = agent
    _install_ordinary_sandbox_guard()
    return authorized_upgrade.public_status(
        authorized_upgrade.route_goal(agent, goal, scope_hints)
    )


_DISPATCH = {
    "request_authorized_self_upgrade": lambda args: request_authorized_self_upgrade(
        args.get("goal", ""),
        args.get("scopes") if isinstance(args.get("scopes"), list) else None,
        args.get("max_files"),
        args.get("max_changed_lines"),
    ),
    "continue_authorized_self_upgrade": lambda args: continue_authorized_self_upgrade(
        args.get("session_id", ""),
        args.get("wait_seconds", 2.0),
    ),
    "authorized_self_upgrade_status": lambda args: authorized_self_upgrade_status(
        args.get("session_id", "")
    ),
    "list_authorized_upgrade_scopes": lambda args: list_authorized_upgrade_scopes(),
}


def execute(tool_name: str, args: dict[str, Any]) -> str:
    handler = _DISPATCH.get(tool_name)
    if handler is None:
        return f"未知工具：{tool_name}"
    try:
        return json.dumps(handler(args or {}), ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps(
            {"status": "failed", "error": f"{type(exc).__name__}: {exc}"},
            ensure_ascii=False,
            indent=2,
        )
