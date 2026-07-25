"""Skill forge: the audited fast lane for capability expansion.

LLM 生成的新技能源码经注册表的语法校验、协议校验（SKILL_META/TOOLS/execute）
与规模/危险模式约束后，写入运行根下 ``app-space/skills`` 并热加载——能力
扩展走“快车道”；核心代码修改仍走 app-tmp → gate → promote 慢车道。
同名覆盖内置技能、触碰 core/* 保护名、卸载内置技能都会被拒绝，注册与
移除全程追加 ``logs/audit.log`` 审计。

快车道测试门禁（可选但推荐）：forge 时附带 ``test_code``，注册表会先把
技能源码与测试源码写入临时目录沙盒跑通（60s 超时），失败/超时即拒绝
注册；通过则写入 ``<name>.tested`` 旁车标记，list 输出中标注 tested。
未附测试不阻断注册，但结果中明确标注“未附测试，建议补充”。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

SKILL_META = {
    "name": "skill_forge",
    "description": "把 LLM 生成的新技能经协议校验后注册到 app-space/skills 并热加载；支持列出与移除，全程审计。",
    "version": "0.1.0",
}

CAPABILITY_META = {
    "id": "agent.skill_forge",
    "name": "技能锻造快车道",
    "description": (
        "在不修改核心代码的前提下扩展能力：新技能写入 app-space/skills，"
        "经 ast 语法校验、临时导入协议校验与规模/危险模式约束后热加载；"
        "可附带测试代码经沙盒验证后注册（快车道质量门禁），未附测试会"
        "明确标注；同名内置技能与 core/* 保护名一律拒绝，操作写入审计日志。"
    ),
    "version": "0.1.0",
    "domain": "agent-governance",
    "operations": [
        {
            "name": "forge_skill",
            "description": "校验并热加载一个新技能到 app-space/skills（可附测试沙盒验证）",
            "risk": "change",
        },
        {
            "name": "list_forged_skills",
            "description": "列出快车道上所有已锻造技能",
            "risk": "read",
        },
        {
            "name": "remove_forged_skill",
            "description": "移除一个快车道技能（内置技能拒绝）",
            "risk": "change",
        },
    ],
    "composes_with": [
        "agent.self_development",
        "agent.self_optimization",
        "agent.self_reflection",
    ],
}

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "forge_skill",
            "description": (
                "把一段新技能源码注册到 app-space/skills 并立即热加载可用。"
                "名称必须是小写字母/数字/下划线，不得与现有技能同名，不得使用 "
                "core/* 等保护名；源码必须通过语法与技能协议校验。"
                "建议同时提供 test_code 验证测试（质量门禁）：测试会在沙盒中"
                "真实运行，跑通才以 tested 状态注册；不附测试也可注册，但会"
                "被明确标注为未验证。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "技能名：小写字母、数字、下划线，且不是保护名。",
                    },
                    "description": {
                        "type": "string",
                        "description": "技能用途说明（写入审计与结果）。",
                    },
                    "source_code": {
                        "type": "string",
                        "description": "完整技能源码，必须含 SKILL_META/TOOLS/execute 协议。",
                    },
                    "test_code": {
                        "type": "string",
                        "description": (
                            "可选但强烈推荐的验证测试（unittest 风格，"
                            "import 技能模块并断言 execute 行为，≤500 行）。"
                            "这是快车道质量门禁：生成技能时应同时生成验证测试；"
                            "测试在沙盒子进程中运行（60s 超时），失败/超时即"
                            "拒绝注册，通过则注册结果标注 tested=true。"
                        ),
                    },
                },
                "required": ["name", "description", "source_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_forged_skills",
            "description": "列出 app-space/skills 下所有已锻造技能（名称/版本/描述）。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_forged_skill",
            "description": (
                "删除 app-space 下的技能文件并从注册表卸载；仅限 app-space 来源，"
                "app/ 内置技能一律拒绝。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "要移除的快车道技能名。",
                    }
                },
                "required": ["name"],
            },
        },
    },
]

_NAME_RE = re.compile(r"[a-z0-9_]+")

# 保护清单：core/* 模块名与 gate 认定的安全关键技能名，防止快车道技能
# 伪装成核心能力或绕过同名检查。
_PROTECTED_NAMES = frozenset(
    {
        # app/core/* 模块
        "agent",
        "autonomy",
        "capabilities",
        "capability_health",
        "configuration",
        "context",
        "llm",
        "local_context",
        "memory",
        "operations",
        "permissions",
        "privacy",
        "registry",
        "self_development",
        "self_optimization",
        "validation",
        # gate_check.sh 保护的安全关键技能
        "evolution_ops",
        "server_ops",
        "software_validation",
        # 快车道自身不可被覆盖
        "skill_forge",
    }
)

_AGENT: Any | None = None
_REGISTRY: Any | None = None


def configure_runtime(*, agent: Any = None, registry: Any = None, **_: Any) -> None:
    global _AGENT, _REGISTRY
    _AGENT = agent
    _REGISTRY = registry


def _registry() -> Any:
    if _REGISTRY is None:
        raise RuntimeError("skill_forge 尚未绑定技能注册表")
    return _REGISTRY


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _appspace_dir() -> str:
    registry = _registry()
    extra = getattr(registry, "extra_skills_dirs", None) or []
    if not extra:
        raise RuntimeError("注册表未配置 app-space 技能目录，快车道不可用")
    return str(extra[0])


def _after_change(name: str | None = None) -> None:
    """Best-effort：绑定运行时并刷新系统提示，绝不影响锻造主流程。"""

    agent = _AGENT
    if agent is None:
        return
    try:
        configure = getattr(agent, "configure_skill_runtimes", None)
        if name and callable(configure):
            configure(name)
        refresh = getattr(agent, "_refresh_system_prompt", None)
        if callable(refresh):
            refresh()
    except Exception:
        pass


def _validate_name(name: str) -> str | None:
    """名称合法性校验；返回拒绝原因或 None。"""

    if not name or not _NAME_RE.fullmatch(name):
        return "技能名只能包含小写字母、数字和下划线"
    if name.startswith("_"):
        return "技能名不能以下划线开头"
    if name in _PROTECTED_NAMES:
        return f"技能名 {name} 在保护清单（core/* 等）中，拒绝锻造"
    if name in _registry().skills:
        return f"技能 {name} 与现有技能同名，拒绝覆盖；如需更新请先移除"
    return None


def _forge_skill(args: dict) -> str:
    name = str(args.get("name", "")).strip()
    description = str(args.get("description", "")).strip()
    source_code = str(args.get("source_code", ""))
    # 测试门禁：可选参数，空白视为未附测试
    test_code = args.get("test_code")
    if test_code is not None:
        test_code = str(test_code)
        if not test_code.strip():
            test_code = None
    if not description:
        return _dump({"forged": False, "message": "技能描述不能为空"})
    if not source_code.strip():
        return _dump({"forged": False, "message": "技能源码不能为空"})
    rejected = _validate_name(name)
    if rejected is not None:
        return _dump({"forged": False, "message": rejected})
    ok, message = _registry().register_external_skill(
        _appspace_dir(), f"{name}.py", source_code, test_code=test_code
    )
    if not ok:
        return _dump({"forged": False, "message": message})
    _after_change(name)
    tested = test_code is not None
    gate_note = "已注册（含测试验证）" if tested else "已注册（未附测试，建议补充）"
    return _dump(
        {
            "forged": True,
            "name": name,
            "origin": "app-space",
            "description": description,
            "tested": tested,
            "message": f"{message}；{gate_note}，新技能可立即通过工具调用使用。",
        }
    )


def _list_forged_skills() -> str:
    registry = _registry()
    appspace_dir: str | None = None  # 惰性解析：无快车道技能时不触碰目录配置
    forged: list[dict[str, Any]] = []
    for name, module in sorted(registry.skills.items()):
        origin_of = getattr(registry, "origin_of", None)
        origin = origin_of(name) if callable(origin_of) else ""
        if origin != "app-space":
            continue
        if appspace_dir is None:
            appspace_dir = _appspace_dir()
        meta = getattr(module, "SKILL_META", {})
        meta = meta if isinstance(meta, dict) else {}
        # tested 标注：以 <name>.tested 旁车标记文件为准
        tested = os.path.exists(os.path.join(appspace_dir, f"{name}.tested"))
        forged.append(
            {
                "name": name,
                "version": str(meta.get("version", "0.0.0")),
                "description": str(meta.get("description", "")),
                "tested": tested,
            }
        )
    return _dump({"origin": "app-space", "count": len(forged), "skills": forged})


def _remove_forged_skill(args: dict) -> str:
    name = str(args.get("name", "")).strip()
    registry = _registry()
    origin_of = getattr(registry, "origin_of", None)
    origin = origin_of(name) if callable(origin_of) else ""
    if origin == "app":
        return _dump(
            {"removed": False, "message": f"技能 {name} 是 app/ 内置技能，拒绝移除"}
        )
    if origin != "app-space":
        return _dump(
            {"removed": False, "message": f"技能 {name} 不在 app-space 快车道上"}
        )
    # 先删文件再卸载注册表；文件缺失时仍完成卸载，保证状态一致
    appspace_dir = _appspace_dir()
    path = os.path.join(appspace_dir, f"{name}.py")
    marker = os.path.join(appspace_dir, f"{name}.tested")
    try:
        if os.path.exists(path):
            os.remove(path)
        # tested 旁车标记一并清理（best-effort）
        if os.path.exists(marker):
            os.remove(marker)
    except OSError as exc:
        return _dump({"removed": False, "message": f"删除技能文件失败: {exc}"})
    ok, message = registry.unregister_external_skill(name)
    if ok:
        _after_change()
    return _dump({"removed": ok, "name": name, "message": message})


def execute(tool_name: str, args: dict) -> str:
    known = {tool["function"]["name"] for tool in TOOLS}
    if tool_name not in known:
        return f"未知工具：{tool_name}，可用工具：{', '.join(sorted(known))}"
    try:
        data = args or {}
        if tool_name == "forge_skill":
            return _forge_skill(data)
        if tool_name == "list_forged_skills":
            return _list_forged_skills()
        return _remove_forged_skill(data)
    except Exception as exc:
        return f"执行失败：{type(exc).__name__}: {exc}"
