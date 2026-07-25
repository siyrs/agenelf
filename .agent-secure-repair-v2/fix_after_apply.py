#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

ROOT = Path.cwd()


def replace_once(path: str, old: str, new: str) -> None:
    file = ROOT / path
    text = file.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one compatibility match, found {count}")
    file.write_text(text.replace(old, new, 1), encoding="utf-8")
    print(f"[compat] {path}")


replace_once(
    "app/tests/test_repair_runner.py",
    "ROOT=Path(__file__).resolve().parents[1]\n",
    "ROOT=Path(__file__).resolve().parents[2]\n",
)
replace_once(
    "app/core/agent.py",
    '"直接技能热加载已默认禁用。外部代码请使用 code.repair 隔离修复；"',
    '"直接技能热加载已禁用（默认）。外部代码请使用 code.repair 隔离修复；"',
)
replace_once(
    "app/skills/skill_forge.py",
    '''    for node in tree.body:\n        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):\n            continue\n        if not isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign, ast.FunctionDef, ast.ClassDef)):\n            return f"{label}顶层仅允许导入、常量和函数/类定义"\n        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.decorator_list:\n            return f"{label}禁止装饰器"\n''',
    '''    for node in tree.body:\n        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):\n            continue\n        if isinstance(node, ast.If):\n            test = node.test\n            safe_main_guard = (\n                isinstance(test, ast.Compare)\n                and isinstance(test.left, ast.Name)\n                and test.left.id == "__name__"\n                and len(test.ops) == 1\n                and isinstance(test.ops[0], ast.Eq)\n                and len(test.comparators) == 1\n                and isinstance(test.comparators[0], ast.Constant)\n                and test.comparators[0].value == "__main__"\n                and not node.orelse\n                and len(node.body) == 1\n                and isinstance(node.body[0], ast.Expr)\n                and isinstance(node.body[0].value, ast.Call)\n                and isinstance(node.body[0].value.func, ast.Attribute)\n                and isinstance(node.body[0].value.func.value, ast.Name)\n                and node.body[0].value.func.value.id == "unittest"\n                and node.body[0].value.func.attr == "main"\n            )\n            if safe_main_guard:\n                continue\n        if not isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign, ast.FunctionDef, ast.ClassDef)):\n            return f"{label}顶层仅允许导入、常量、函数/类定义和标准 unittest.main 守卫"\n        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.decorator_list:\n            return f"{label}禁止装饰器"\n''',
)
replace_once(
    "app/skills/skill_forge.py",
    '''    registry = _registry()\n    if getattr(registry, "origin_of", lambda _: "")(name) != "app-space":\n        return _dump({"removed": False, "message": f"技能 {name} 不在 app-space 快车道上"})\n    appspace = _appspace_dir()\n''',
    '''    registry = _registry()\n    origin = getattr(registry, "origin_of", lambda _: "")(name)\n    if name in registry.skills and origin != "app-space":\n        return _dump({"removed": False, "message": f"内置技能 {name} 不能由 skill_forge 移除"})\n    if origin != "app-space":\n        return _dump({"removed": False, "message": f"技能 {name} 不在 app-space 快车道上"})\n    appspace = _appspace_dir()\n''',
)
replace_once(
    "app/core/registry.py",
    'return True, f"{message}；测试验证通过（tested=true）"',
    'return True, f"{message}；已注册（含测试验证）；测试验证通过（tested=true）"',
)
print("compatibility fixes applied")
