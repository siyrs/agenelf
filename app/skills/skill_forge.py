"""Explicitly opt-in experimental skill forge.

Dynamic Python modules are executable code, not data.  Therefore Agenelf no longer
loads or forges ``app-space`` skills by default.  Both
``AGENELF_ENABLE_APP_SPACE_SKILLS=1`` and ``AGENELF_ENABLE_SKILL_FORGE=1`` are
required, every candidate must include tests, and a conservative AST policy rejects
filesystem, process, network, reflection and dynamic-code primitives.  Core changes
still belong in the app-tmp -> tests -> gate -> host-promotion pipeline.
"""
from __future__ import annotations

import ast
import json
import os
import re
from typing import Any

SKILL_META = {
    "name": "skill_forge",
    "description": "默认关闭的实验性技能锻造；仅在主人显式开启、附测试并通过静态安全规则后写入 app-space。",
    "version": "1.0.0",
}

CAPABILITY_META = {
    "id": "agent.skill_forge",
    "name": "实验性技能锻造",
    "description": (
        "默认关闭。主人显式开启后，新技能仍必须附测试并通过保守 AST 检查；"
        "不适用于核心、权限、Runner、凭据或外部副作用能力。"
    ),
    "version": "1.0.0",
    "domain": "agent-governance",
    "operations": [
        {"name": "forge_skill", "description": "实验性注册受限纯计算技能", "risk": "change"},
        {"name": "list_forged_skills", "description": "列出已加载的 app-space 技能", "risk": "read"},
        {"name": "remove_forged_skill", "description": "移除 app-space 技能", "risk": "change"},
    ],
    "composes_with": ["agent.self_development", "code.repair"],
}

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "forge_skill",
            "description": (
                "默认关闭。仅用于主人显式启用的实验性纯计算技能；必须同时提供 unittest 测试。"
                "文件、网络、进程、动态导入、任意代码执行和核心能力均被拒绝。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "source_code": {"type": "string"},
                    "test_code": {"type": "string"},
                },
                "required": ["name", "description", "source_code", "test_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_forged_skills",
            "description": "列出当前已加载 app-space 技能及 tested 标记。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_forged_skill",
            "description": "移除一个已加载的 app-space 技能；内置技能拒绝。",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
]

_NAME_RE = re.compile(r"[a-z0-9_]+")
_PROTECTED_NAMES = frozenset(
    {
        "agent",
        "autonomy",
        "capabilities",
        "capability_health",
        "channel_envelope",
        "code_repair",
        "configuration",
        "context",
        "llm",
        "local_context",
        "memory",
        "model_router",
        "operations",
        "permissions",
        "privacy",
        "registry",
        "self_development",
        "self_optimization",
        "task_engine",
        "validation",
        "evolution_ops",
        "server_ops",
        "software_validation",
        "workflow_tasks",
        "skill_forge",
    }
)
_FORBIDDEN_MODULES = frozenset(
    {
        "asyncio",
        "builtins",
        "ctypes",
        "http",
        "importlib",
        "inspect",
        "multiprocessing",
        "os",
        "pathlib",
        "pickle",
        "requests",
        "shutil",
        "signal",
        "socket",
        "subprocess",
        "sys",
        "tempfile",
        "threading",
        "urllib",
    }
)
_FORBIDDEN_CALLS = frozenset(
    {"open", "eval", "exec", "compile", "__import__", "input", "breakpoint"}
)
_AGENT: Any | None = None
_REGISTRY: Any | None = None


def configure_runtime(*, agent: Any = None, registry: Any = None, **_: Any) -> None:
    global _AGENT, _REGISTRY
    _AGENT = agent
    _REGISTRY = registry
    # 优先把绑定状态写到 Registry 实例的 per-instance 上下文，
    # 模块级全局仅作未传入 registry 时的兼容兜底
    if registry is not None and hasattr(registry, "bind_state"):
        registry.bind_state("skill_forge", agent=agent, registry=registry)


def _enabled() -> bool:
    return (
        os.environ.get("AGENELF_ENABLE_APP_SPACE_SKILLS", "0") == "1"
        and os.environ.get("AGENELF_ENABLE_SKILL_FORGE", "0") == "1"
    )


def _registry() -> Any:
    registry = _REGISTRY
    if registry is not None and hasattr(registry, "get_state"):
        bound = registry.get_state("skill_forge").get("registry")
        if bound is not None:
            return bound
    if registry is None:
        raise RuntimeError("skill_forge 尚未绑定技能注册表")
    return registry


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _appspace_dir() -> str:
    extra = getattr(_registry(), "extra_skills_dirs", None) or []
    if not extra:
        raise RuntimeError("app-space 自动加载未启用")
    return str(extra[0])


def _validate_name(name: str) -> str | None:
    if not name or not _NAME_RE.fullmatch(name):
        return "技能名只能包含小写字母、数字和下划线"
    if name.startswith("_"):
        return "技能名不能以下划线开头"
    if name in _PROTECTED_NAMES:
        return f"技能名 {name} 在保护清单中，拒绝锻造"
    if name in _registry().skills:
        return f"技能 {name} 与现有技能同名，拒绝覆盖"
    return None


def _static_policy(source: str, label: str) -> str | None:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return f"{label}语法校验失败：{exc}"
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        if isinstance(node, ast.If):
            test = node.test
            safe_main_guard = (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id == "__name__"
                and len(test.ops) == 1
                and isinstance(test.ops[0], ast.Eq)
                and len(test.comparators) == 1
                and isinstance(test.comparators[0], ast.Constant)
                and test.comparators[0].value == "__main__"
                and not node.orelse
                and len(node.body) == 1
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Call)
                and isinstance(node.body[0].value.func, ast.Attribute)
                and isinstance(node.body[0].value.func.value, ast.Name)
                and node.body[0].value.func.value.id == "unittest"
                and node.body[0].value.func.attr == "main"
            )
            if safe_main_guard:
                continue
        if not isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign, ast.FunctionDef, ast.ClassDef)):
            return f"{label}顶层仅允许导入、常量、函数/类定义和标准 unittest.main 守卫"
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.decorator_list:
            return f"{label}禁止装饰器"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _FORBIDDEN_MODULES:
                    return f"{label}禁止导入模块：{alias.name}"
        elif isinstance(node, ast.ImportFrom):
            module = str(node.module or "").split(".")[0]
            if module in _FORBIDDEN_MODULES:
                return f"{label}禁止导入模块：{node.module}"
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _FORBIDDEN_CALLS:
                return f"{label}禁止调用：{node.func.id}"
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            return f"{label}禁止访问双下划线属性"
    return None


def _after_change(name: str | None = None) -> None:
    agent = None
    if _REGISTRY is not None and hasattr(_REGISTRY, "get_state"):
        agent = _REGISTRY.get_state("skill_forge").get("agent")
    if agent is None:
        agent = _AGENT
    if agent is None:
        return
    try:
        if name:
            agent.configure_skill_runtimes(name)
        agent._refresh_system_prompt()
    except Exception:
        pass


def _forge(args: dict[str, Any]) -> str:
    if not _enabled():
        return _dump(
            {
                "forged": False,
                "disabled": True,
                "message": (
                    "技能锻造默认关闭。确需实验时，主人必须同时设置 "
                    "AGENELF_ENABLE_APP_SPACE_SKILLS=1 与 AGENELF_ENABLE_SKILL_FORGE=1；"
                    "核心改动请使用受控自主迭代或 code.repair。"
                ),
            }
        )
    name = str(args.get("name", "")).strip()
    description = str(args.get("description", "")).strip()
    source = str(args.get("source_code", ""))
    tests = str(args.get("test_code", ""))
    if not description or not source.strip() or not tests.strip():
        return _dump({"forged": False, "message": "description、source_code 和 test_code 均不能为空"})
    rejected = _validate_name(name) or _static_policy(source, "技能源码") or _static_policy(tests, "测试代码")
    if rejected:
        return _dump({"forged": False, "message": rejected})
    ok, message = _registry().register_external_skill(
        _appspace_dir(), f"{name}.py", source, test_code=tests
    )
    if not ok:
        return _dump({"forged": False, "message": message})
    _after_change(name)
    return _dump(
        {
            "forged": True,
            "name": name,
            "origin": "app-space",
            "tested": True,
            "description": description[:500],
            "message": message,
        }
    )


def _list() -> str:
    registry = _registry()
    forged: list[dict[str, Any]] = []
    for name, module in sorted(registry.skills.items()):
        if getattr(registry, "origin_of", lambda _: "")(name) != "app-space":
            continue
        meta = getattr(module, "SKILL_META", {})
        forged.append(
            {
                "name": name,
                "version": str(meta.get("version", "0.0.0")) if isinstance(meta, dict) else "0.0.0",
                "description": str(meta.get("description", "")) if isinstance(meta, dict) else "",
                "tested": True,
            }
        )
    return _dump({"enabled": _enabled(), "origin": "app-space", "count": len(forged), "skills": forged})


def _remove(args: dict[str, Any]) -> str:
    if not _enabled():
        return _dump({"removed": False, "disabled": True, "message": "app-space 技能未启用"})
    name = str(args.get("name", "")).strip()
    registry = _registry()
    origin = getattr(registry, "origin_of", lambda _: "")(name)
    if name in registry.skills and origin != "app-space":
        return _dump({"removed": False, "message": f"内置技能 {name} 不能由 skill_forge 移除"})
    if origin != "app-space":
        return _dump({"removed": False, "message": f"技能 {name} 不在 app-space 快车道上"})
    appspace = _appspace_dir()
    path = os.path.join(appspace, f"{name}.py")
    marker = os.path.join(appspace, f"{name}.tested")
    try:
        if os.path.exists(path):
            os.remove(path)
        if os.path.exists(marker):
            os.remove(marker)
    except OSError as exc:
        return _dump({"removed": False, "message": f"删除技能文件失败：{exc}"})
    ok, message = registry.unregister_external_skill(name)
    _after_change()
    return _dump({"removed": ok, "message": message})


def execute(tool_name: str, args: dict) -> str:
    try:
        if tool_name == "forge_skill":
            return _forge(args or {})
        if tool_name == "list_forged_skills":
            return _list()
        if tool_name == "remove_forged_skill":
            return _remove(args or {})
        return f"未知工具：{tool_name}"
    except Exception as exc:
        return f"执行失败：{type(exc).__name__}: {exc}"
