"""Skill registry with an optional composable capability catalog."""

from __future__ import annotations

import ast
import importlib.util
import os
import sys
import traceback
from typing import Any

from .capabilities import normalize_capability_meta

_REQUIRED_ATTRS = ("SKILL_META", "TOOLS", "execute")


class SkillRegistry:
    """Discover, validate, reload and dispatch skill modules."""

    def __init__(self, skills_dir: str):
        self.skills_dir = os.path.abspath(skills_dir)
        self.skills: dict[str, object] = {}
        self.errors: dict[str, str] = {}
        self._tool_index: dict[str, str] = {}

    @staticmethod
    def _skill_name_from_file(filename: str) -> str:
        return os.path.splitext(os.path.basename(filename))[0]

    def _load_module(self, path: str):
        name = self._skill_name_from_file(path)
        module_key = f"agenelf_skill_{name}"
        spec = importlib.util.spec_from_file_location(module_key, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法为技能 {name} 创建加载规格")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_key] = module
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _validate_module(module) -> None:
        for attr in _REQUIRED_ATTRS:
            if not hasattr(module, attr):
                raise AttributeError(f"技能缺少协议要求的属性: {attr}")
        if not isinstance(module.SKILL_META, dict):
            raise TypeError("技能 SKILL_META 必须是 dict")
        if not isinstance(module.TOOLS, list):
            raise TypeError("技能 TOOLS 必须是 list")
        if not callable(module.execute):
            raise TypeError("技能 execute 必须可调用")
        seen: set[str] = set()
        for tool in module.TOOLS:
            if not isinstance(tool, dict) or tool.get("type") != "function":
                raise TypeError("TOOLS 每项必须是 function schema")
            function = tool.get("function")
            if not isinstance(function, dict):
                raise TypeError("tool.function 必须是对象")
            name = str(function.get("name", "")).strip()
            if not name:
                raise ValueError("tool name 不能为空")
            if name in seen:
                raise ValueError(f"技能内部工具名重复：{name}")
            seen.add(name)
            parameters = function.get("parameters")
            if not isinstance(parameters, dict) or parameters.get("type") != "object":
                raise TypeError(f"工具 {name} 的 parameters 必须是 object schema")

    def _register_module(self, name: str, module) -> None:
        old = self.skills.get(name)
        self.skills[name] = module
        try:
            self._rebuild_tool_index()
            self._descriptor_for(name, module)
        except Exception:
            if old is None:
                self.skills.pop(name, None)
            else:
                self.skills[name] = old
            self._rebuild_tool_index()
            raise
        self.errors.pop(name, None)

    def _rebuild_tool_index(self) -> None:
        index: dict[str, str] = {}
        for skill_name, module in self.skills.items():
            for tool in getattr(module, "TOOLS", []):
                function = tool.get("function", {}) if isinstance(tool, dict) else {}
                tool_name = function.get("name")
                if not tool_name:
                    continue
                previous = index.get(tool_name)
                if previous is not None and previous != skill_name:
                    raise ValueError(
                        f"工具名冲突：{tool_name} 同时由 {previous} 与 {skill_name} 提供"
                    )
                index[tool_name] = skill_name
        self._tool_index = index

    def _descriptor_for(self, name: str, module) -> dict[str, Any]:
        tool_names = [
            str(tool.get("function", {}).get("name"))
            for tool in getattr(module, "TOOLS", [])
            if tool.get("function", {}).get("name")
        ]
        descriptor = normalize_capability_meta(
            skill_name=name,
            skill_meta=getattr(module, "SKILL_META", {}),
            capability_meta=getattr(module, "CAPABILITY_META", None),
            tool_names=tool_names,
        )
        return descriptor.as_dict()

    def discover(self) -> list[str]:
        if not os.path.isdir(self.skills_dir):
            return []
        for filename in sorted(os.listdir(self.skills_dir)):
            if not filename.endswith(".py") or filename.startswith("_"):
                continue
            name = self._skill_name_from_file(filename)
            path = os.path.join(self.skills_dir, filename)
            try:
                module = self._load_module(path)
                self._validate_module(module)
                self._register_module(name, module)
            except Exception:
                self.errors[name] = traceback.format_exc(limit=5)
        return list(self.skills.keys())

    def reload(self, name: str) -> bool:
        path = os.path.join(self.skills_dir, f"{name}.py")
        if not os.path.exists(path):
            return False
        old = self.skills.get(name)
        try:
            module = self._load_module(path)
            self._validate_module(module)
            self._register_module(name, module)
            return True
        except Exception:
            self.errors[name] = traceback.format_exc(limit=5)
            if old is None:
                self.skills.pop(name, None)
            else:
                self.skills[name] = old
            try:
                self._rebuild_tool_index()
            except Exception:
                self._tool_index = {}
            return False

    def all_tool_schemas(self) -> list[dict]:
        schemas: list[dict] = []
        for module in self.skills.values():
            schemas.extend(getattr(module, "TOOLS", []))
        return schemas

    def capability_catalog(self) -> list[dict[str, Any]]:
        """Return stable capability-domain metadata for planning and UI."""

        catalog = [
            self._descriptor_for(name, module)
            for name, module in sorted(self.skills.items())
        ]
        catalog.sort(key=lambda item: (item["domain"], item["id"]))
        return catalog

    def dispatch(self, tool_name: str, args: dict) -> str:
        skill_name = self._tool_index.get(tool_name)
        if skill_name is None:
            return f"错误：未知工具 {tool_name}"
        module = self.skills[skill_name]
        try:
            return str(module.execute(tool_name, args or {}))
        except Exception:
            return f"错误：工具 {tool_name} 执行异常\n{traceback.format_exc(limit=3)}"

    def register_new_skill(self, filename: str, source_code: str) -> tuple[bool, str]:
        filename = os.path.basename(filename)
        if not filename.endswith(".py"):
            filename += ".py"
        if filename.startswith("_"):
            return False, "技能文件名不能以下划线开头"
        path = os.path.join(self.skills_dir, filename)
        try:
            ast.parse(source_code)
        except SyntaxError as exc:
            return False, f"语法校验失败: {exc}"

        os.makedirs(self.skills_dir, exist_ok=True)
        written = False
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(source_code)
            written = True
            module = self._load_module(path)
            self._validate_module(module)
            name = self._skill_name_from_file(filename)
            self._register_module(name, module)
            return True, f"技能 {name} 注册成功"
        except Exception as exc:
            if written and os.path.exists(path):
                os.remove(path)
            return False, f"技能注册失败: {exc}"
