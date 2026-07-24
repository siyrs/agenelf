"""技能注册中心模块。

技能协议：skills/ 目录下的每个 .py 文件即一个技能，模块级必须定义：
- SKILL_META = {"name": ..., "description": ..., "version": ...}
- TOOLS: list[dict]   # OpenAI function-calling schema 列表
- def execute(tool_name: str, args: dict) -> str  # 内部捕获所有异常，返回字符串

SkillRegistry 负责发现、热重载、分发调用以及动态注册新技能。
"""

from __future__ import annotations

import ast
import importlib.util
import os
import sys
import traceback

# 技能模块必须满足的协议字段，用于注册前校验
_REQUIRED_ATTRS = ("SKILL_META", "TOOLS", "execute")


class SkillRegistry:
    """技能注册中心：扫描、加载、热重载与分发技能工具。"""

    def __init__(self, skills_dir: str):
        # 技能目录的绝对路径；加载失败的技能记录在此（技能名 -> 错误信息）
        self.skills_dir = os.path.abspath(skills_dir)
        self.skills: dict[str, object] = {}
        self.errors: dict[str, str] = {}
        # 工具名 -> 所属技能名，用于 dispatch 路由
        self._tool_index: dict[str, str] = {}

    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------
    def _skill_name_from_file(self, filename: str) -> str:
        """由文件名得到技能名（去掉 .py 后缀）。"""
        return os.path.splitext(os.path.basename(filename))[0]

    def _load_module(self, path: str):
        """从文件路径加载技能模块（独立模块命名空间，避免冲突）。"""
        name = self._skill_name_from_file(path)
        # 使用带路径的唯一模块名，便于热重载时不与旧模块混淆
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
        """校验模块是否满足技能协议，缺项则抛 AttributeError。"""
        for attr in _REQUIRED_ATTRS:
            if not hasattr(module, attr):
                raise AttributeError(f"技能缺少协议要求的属性: {attr}")
        if not isinstance(module.TOOLS, list):
            raise TypeError("技能 TOOLS 必须是 list")
        if not callable(module.execute):
            raise TypeError("技能 execute 必须可调用")

    def _register_module(self, name: str, module) -> None:
        """把通过校验的模块登记进注册表并重建工具索引。"""
        self.skills[name] = module
        self.errors.pop(name, None)
        self._rebuild_tool_index()

    def _rebuild_tool_index(self) -> None:
        """根据当前已加载技能重建 工具名 -> 技能名 索引。"""
        self._tool_index.clear()
        for skill_name, module in self.skills.items():
            for tool in getattr(module, "TOOLS", []):
                fn = tool.get("function", {}) if isinstance(tool, dict) else {}
                tool_name = fn.get("name")
                if tool_name:
                    self._tool_index[tool_name] = skill_name

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def discover(self) -> list[str]:
        """扫描技能目录并加载所有 .py 技能，返回成功加载的技能名列表。"""
        if not os.path.isdir(self.skills_dir):
            return []
        for filename in sorted(os.listdir(self.skills_dir)):
            if not filename.endswith(".py") or filename.startswith("_"):
                # 跳过 __init__.py 等非技能文件
                continue
            name = self._skill_name_from_file(filename)
            path = os.path.join(self.skills_dir, filename)
            try:
                module = self._load_module(path)
                self._validate_module(module)
                self._register_module(name, module)
            except Exception:
                # 单个技能加载失败不影响其他技能，记录错误原因
                self.errors[name] = traceback.format_exc(limit=3)
        return list(self.skills.keys())

    def reload(self, name: str) -> bool:
        """热重载单个技能：重新从磁盘加载同名文件。成功返回 True。"""
        path = os.path.join(self.skills_dir, f"{name}.py")
        if not os.path.exists(path):
            return False
        try:
            module = self._load_module(path)
            self._validate_module(module)
            self._register_module(name, module)
            return True
        except Exception:
            self.errors[name] = traceback.format_exc(limit=3)
            # 重载失败时移除旧版本，避免分发到过期实现
            self.skills.pop(name, None)
            self._rebuild_tool_index()
            return False

    def all_tool_schemas(self) -> list[dict]:
        """汇总所有技能的 OpenAI function-calling schema。"""
        schemas: list[dict] = []
        for module in self.skills.values():
            schemas.extend(getattr(module, "TOOLS", []))
        return schemas

    def dispatch(self, tool_name: str, args: dict) -> str:
        """按工具名路由到对应技能的 execute；任何异常都转为字符串返回。"""
        skill_name = self._tool_index.get(tool_name)
        if skill_name is None:
            return f"错误：未知工具 {tool_name}"
        module = self.skills[skill_name]
        try:
            result = module.execute(tool_name, args or {})
            return str(result)
        except Exception:
            # 协议要求技能内部自捕获异常，这里兜底防御
            return f"错误：工具 {tool_name} 执行异常\n{traceback.format_exc(limit=3)}"

    def register_new_skill(self, filename: str, source_code: str) -> tuple[bool, str]:
        """动态注册新技能。

        流程：写入文件 → ast 语法校验 → 临时导入校验协议完整性 → 注册。
        任一步失败返回 (False, 原因)，并清理已写入的文件，不留垃圾。
        """
        # 统一文件名：必须是不带路径的 .py 文件
        filename = os.path.basename(filename)
        if not filename.endswith(".py"):
            filename += ".py"
        if filename.startswith("_"):
            return False, "技能文件名不能以下划线开头"
        path = os.path.join(self.skills_dir, filename)

        # 先做 ast 语法校验，失败则根本不会写文件
        try:
            ast.parse(source_code)
        except SyntaxError as e:
            return False, f"语法校验失败: {e}"

        os.makedirs(self.skills_dir, exist_ok=True)
        written = False
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(source_code)
            written = True

            # 临时导入并校验协议完整性
            module = self._load_module(path)
            self._validate_module(module)

            name = self._skill_name_from_file(filename)
            self._register_module(name, module)
            return True, f"技能 {name} 注册成功"
        except Exception as e:
            # 失败时删除已写入的文件，保持目录干净
            if written and os.path.exists(path):
                os.remove(path)
            return False, f"技能注册失败: {e}"
