"""Skill registry with an optional composable capability catalog.

除主目录 ``app/skills`` 外，注册表还可以扫描运行根下的额外技能目录
（``app-space/skills``，容器内可写挂载）。这是能力扩展的“快车道”：
新技能经协议校验后写入 app-space 并热加载；同名技能始终由主目录优先，
核心代码改动仍走 app-tmp → gate → promote 慢车道。
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from typing import Any

from .capabilities import normalize_capability_meta
from .execution_policy import audit_dispatch, evaluate_contract, resolve_contract

_REQUIRED_ATTRS = ("SKILL_META", "TOOLS", "execute")

# 快车道来源标注：主目录技能为 "app"，额外目录技能为 "app-space"
ORIGIN_APP = "app"
ORIGIN_APP_SPACE = "app-space"

# 快车道单文件规模约束（与 gate_check.sh 的规模限值同族）
_EXTERNAL_MAX_LINES = 500
_EXTERNAL_MAX_CHARS = 64_000

# 快车道测试门禁：测试代码规模上限与沙盒运行默认超时（秒）
_TEST_MAX_LINES = 500
_TEST_DEFAULT_TIMEOUT = 60.0
# 测试失败时返回给调用方的输出尾部截断长度
_TEST_OUTPUT_TAIL = 1500

# 快车道危险模式（与 scripts/gate_check.sh 检查 a/6 同族，注册时即拒绝）
_DANGEROUS_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern)
    for pattern in (
        r"rm\s+-rf\s+/([\s\"';|&)]|$)",
        r"mkfs",
        r":\(\)\s*\{\s*:\|:\s*&\s*\}\s*;\s*:",
        r"(>{1,2}|tee\s+)\s*/etc/passwd",
        r"open\([^)]*/etc/passwd[^)]*['\"][wa]",
        r"docker\.sock",
        r"curl[^|]*\|\s*(sudo\s+)?(ba)?sh(\s|$)",
        r"sk-[a-zA-Z0-9]{20,}",
    )
)


class SkillRegistry:
    """Discover, validate, reload and dispatch skill modules."""

    def __init__(
        self,
        skills_dir: str,
        extra_skills_dirs: list[str] | None = None,
        *,
        policy_engine: Any | None = None,
    ):
        self.skills_dir = os.path.abspath(skills_dir)
        # 额外技能目录（快车道），约定第一个为 app-space/skills 可写目录
        self.extra_skills_dirs: list[str] = [
            os.path.abspath(path) for path in (extra_skills_dirs or [])
        ]
        self.skills: dict[str, object] = {}
        self.errors: dict[str, str] = {}
        self._tool_index: dict[str, str] = {}
        # 技能名 -> 来源（app / app-space），用于清单标注与越权保护
        self._origins: dict[str, str] = {}
        self.policy_engine = policy_engine
        self._contracts: dict[str, Any | None] = {}

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
        contracts: dict[str, Any | None] = {}
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
                contracts[tool_name] = resolve_contract(str(tool_name), {}, module)
        self._tool_index = index
        self._contracts = contracts

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

    def _scan_dir(self, directory: str, *, origin: str) -> None:
        """扫描单个技能目录；同名技能先到先得（主目录先于 extra 扫描）。"""

        if not os.path.isdir(directory):
            return
        for filename in sorted(os.listdir(directory)):
            if not filename.endswith(".py") or filename.startswith("_"):
                continue
            name = self._skill_name_from_file(filename)
            if name in self.skills:
                # 主目录优先：app/ 中的同名技能覆盖 app-space/
                continue
            path = os.path.join(directory, filename)
            try:
                module = self._load_module(path)
                self._validate_module(module)
                self._register_module(name, module)
                self._origins[name] = origin
            except Exception:
                self.errors[name] = traceback.format_exc(limit=5)

    def discover(self) -> list[str]:
        self._scan_dir(self.skills_dir, origin=ORIGIN_APP)
        for extra_dir in self.extra_skills_dirs:
            self._scan_dir(extra_dir, origin=ORIGIN_APP_SPACE)
        return list(self.skills.keys())

    def origin_of(self, name: str) -> str:
        """返回技能来源：app（内置）或 app-space（快车道）；未知返回空串。"""

        return self._origins.get(name, "")

    def reload(self, name: str) -> bool:
        candidates = [os.path.join(self.skills_dir, f"{name}.py")]
        candidates.extend(
            os.path.join(extra_dir, f"{name}.py")
            for extra_dir in self.extra_skills_dirs
        )
        path = next((item for item in candidates if os.path.exists(item)), None)
        if path is None:
            return False
        origin = (
            ORIGIN_APP
            if os.path.dirname(os.path.abspath(path)) == self.skills_dir
            else ORIGIN_APP_SPACE
        )
        old = self.skills.get(name)
        old_origin = self._origins.get(name)
        try:
            module = self._load_module(path)
            self._validate_module(module)
            self._register_module(name, module)
            self._origins[name] = origin
            return True
        except Exception:
            self.errors[name] = traceback.format_exc(limit=5)
            if old is None:
                self.skills.pop(name, None)
                self._origins.pop(name, None)
            else:
                self.skills[name] = old
                if old_origin is not None:
                    self._origins[name] = old_origin
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
        """Return stable capability-domain metadata for planning and UI.

        每条描述附带 ``origin``：内置技能为 ``app``，快车道技能为
        ``app-space``，便于规划与审计区分能力来源。
        """

        catalog: list[dict[str, Any]] = []
        for name, module in sorted(self.skills.items()):
            descriptor = self._descriptor_for(name, module)
            descriptor["origin"] = self._origins.get(name, ORIGIN_APP)
            tool_names = [
                str(tool.get("function", {}).get("name"))
                for tool in getattr(module, "TOOLS", [])
                if tool.get("function", {}).get("name")
            ]
            descriptor["tool_contracts"] = [
                self._contracts[tool_name].as_dict()
                for tool_name in tool_names
                if self._contracts.get(tool_name) is not None
            ]
            descriptor["unclassified_tools"] = [
                tool_name for tool_name in tool_names if self._contracts.get(tool_name) is None
            ]
            catalog.append(descriptor)
        catalog.sort(key=lambda item: (item["domain"], item["id"]))
        return catalog

    def contract_for(self, tool_name: str, args: dict | None = None):
        skill_name = self._tool_index.get(tool_name)
        if skill_name is None:
            return None
        return resolve_contract(tool_name, args or {}, self.skills[skill_name])

    def unclassified_tools(self) -> list[str]:
        return sorted(name for name, contract in self._contracts.items() if contract is None)

    def dispatch(self, tool_name: str, args: dict, *, subject: str = "agent") -> str:
        skill_name = self._tool_index.get(tool_name)
        if skill_name is None:
            return f"错误：未知工具 {tool_name}"
        module = self.skills[skill_name]
        contract = resolve_contract(tool_name, args or {}, module)
        decision = evaluate_contract(self.policy_engine, contract, subject)
        audit_dispatch(tool_name, contract, subject, decision)
        if not decision.get("allowed", False):
            return f"错误：策略拒绝工具 {tool_name}：{decision.get('reason', '未说明原因')}"
        try:
            return str(module.execute(tool_name, args or {}))
        except Exception:
            return f"错误：工具 {tool_name} 执行异常\n{traceback.format_exc(limit=3)}"

    def register_new_skill(self, filename: str, source_code: str) -> tuple[bool, str]:
        if os.environ.get("AGENELF_ENABLE_DIRECT_SKILL_REGISTRATION", "0") != "1":
            return False, (
                "直接向 app/skills 写入并热加载模型生成代码已禁用；"
                "请使用 code.repair 或受控 app-tmp 晋升流程"
            )
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

    # ------------------------------------------------------------------
    # app-space 快车道：只写 extra 目录第一个（app-space/skills）
    # ------------------------------------------------------------------
    @staticmethod
    def _check_external_source(source_code: str) -> str | None:
        """规模与危险模式约束（与 gate 同族）；返回拒绝原因或 None。"""

        if len(source_code) > _EXTERNAL_MAX_CHARS:
            return f"源码大小 {len(source_code)} 字符超过快车道上限 {_EXTERNAL_MAX_CHARS}"
        line_count = source_code.count("\n") + 1
        if line_count > _EXTERNAL_MAX_LINES:
            return f"源码行数 {line_count} 超过快车道上限 {_EXTERNAL_MAX_LINES}"
        for pattern in _DANGEROUS_PATTERNS:
            if pattern.search(source_code):
                return f"源码命中危险模式：{pattern.pattern}"
        return None

    @staticmethod
    def _check_test_source(test_code: str) -> str | None:
        """测试代码的注册前校验；返回拒绝原因或 None。

        测试代码会在沙盒子进程中真实执行，因此除 ast 语法与规模限制
        （≤500 行 / ≤64K 字符）外，还套用与技能源码同族的危险模式扫描，
        防止测试通道绕过快车道的安全底线。
        """

        try:
            ast.parse(test_code)
        except SyntaxError as exc:
            return f"测试代码语法校验失败: {exc}"
        if len(test_code) > _EXTERNAL_MAX_CHARS:
            return (
                f"测试代码大小 {len(test_code)} 字符超过快车道上限 "
                f"{_EXTERNAL_MAX_CHARS}"
            )
        line_count = test_code.count("\n") + 1
        if line_count > _TEST_MAX_LINES:
            return f"测试代码行数 {line_count} 超过快车道上限 {_TEST_MAX_LINES}"
        for pattern in _DANGEROUS_PATTERNS:
            if pattern.search(test_code):
                return f"测试代码命中危险模式：{pattern.pattern}"
        return None

    @staticmethod
    def _run_forge_tests(
        name: str, filename: str, source_code: str, test_code: str, timeout: float
    ) -> str | None:
        """沙盒运行测试：技能源码 + 测试源码写入临时目录后 subprocess 执行。

        技能文件名与注册目标一致，保证测试里 ``import <name>`` 可用；
        测试文件为 ``test_<name>.py``，以 ``python -m unittest`` 运行，
        PYTHONPATH 指向临时目录。返回拒绝原因（失败/超时）或 None（通过）；
        临时目录随上下文管理器销毁，不留垃圾。
        """

        with tempfile.TemporaryDirectory(prefix="agenelf-forge-test-") as sandbox:
            with open(
                os.path.join(sandbox, filename), "w", encoding="utf-8"
            ) as handle:
                handle.write(source_code)
            test_module = f"test_{name}"
            with open(
                os.path.join(sandbox, f"{test_module}.py"), "w", encoding="utf-8"
            ) as handle:
                handle.write(test_code)
            # Do not inherit Agent credentials, provider keys, proxies or
            # owner-local paths into experimental test subprocesses.
            env = {
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "PYTHONPATH": sandbox,
                "HOME": sandbox,
                "TMPDIR": sandbox,
                "PYTHONIOENCODING": "utf-8",
                "PYTHONDONTWRITEBYTECODE": "1",
                "CI": "1",
                "NO_PROXY": "*",
                "no_proxy": "*",
                "HTTP_PROXY": "",
                "HTTPS_PROXY": "",
                "ALL_PROXY": "",
            }
            try:
                proc = subprocess.run(
                    [sys.executable, "-m", "unittest", test_module],
                    cwd=sandbox,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                return f"测试验证超时（>{timeout:g}s），拒绝注册"
            except OSError as exc:
                return f"测试沙盒启动失败: {exc}"
            if proc.returncode != 0:
                output = ((proc.stdout or "") + (proc.stderr or "")).strip()
                tail = output[-_TEST_OUTPUT_TAIL:] if output else "（无输出）"
                return (
                    f"测试验证失败（exit={proc.returncode}），拒绝注册。"
                    f"尾部输出：\n{tail}"
                )
        return None

    @staticmethod
    def _tested_marker_path(target_dir: str, name: str) -> str:
        """tested 旁车标记：app-space/skills/<name>.tested。"""

        return os.path.join(target_dir, f"{name}.tested")

    def _write_tested_marker(self, target_dir: str, name: str) -> None:
        """Best-effort 写入 tested 标记（记录验证时间），失败不影响注册。"""

        try:
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with open(
                self._tested_marker_path(target_dir, name), "w", encoding="utf-8"
            ) as handle:
                handle.write(f"tested=true verified_at={stamp}\n")
        except OSError:
            pass

    def _audit_forge(self, detail: str) -> None:
        """Best-effort 审计：写入运行根 logs/audit.log，失败绝不影响主流程。"""

        if not self.extra_skills_dirs:
            return
        # app-space/skills 的上两级即运行根（与 docker-compose 挂载一致）
        root = os.path.dirname(os.path.dirname(self.extra_skills_dirs[0]))
        path = os.path.join(root, "logs", "audit.log")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(f"[{stamp}] [skill_forge] {detail}\n")
        except OSError:
            pass

    def register_external_skill(
        self,
        dirname: str,
        filename: str,
        source_code: str,
        test_code: str | None = None,
        test_timeout: float = _TEST_DEFAULT_TIMEOUT,
    ) -> tuple[bool, str]:
        """快车道注册：校验后写入 app-space/skills 并热加载。

        与 ``register_new_skill`` 使用同一套 ast 语法校验与临时导入协议
        校验（SKILL_META/TOOLS/execute），失败不留垃圾文件；区别是只允许
        写入 extra 目录第一个（app-space/skills），绝不触碰主目录。

        可选的 ``test_code`` 是快车道测试门禁：提供时先在临时目录沙盒中
        运行测试（``import <name>`` 可用，60s 超时，可用 ``test_timeout``
        调整），失败/超时即拒绝注册且不留文件；通过则走正常注册流程并
        写入 ``<name>.tested`` 旁车标记。未提供时不阻断注册，但结果中
        明确标注“未附测试”。
        """

        if not self.extra_skills_dirs:
            return False, "未配置 app-space 技能目录，快车道不可用"
        target_dir = os.path.abspath(str(dirname))
        if target_dir != self.extra_skills_dirs[0]:
            return False, (
                f"越权路径：快车道只允许写入 {self.extra_skills_dirs[0]}"
            )
        filename = os.path.basename(filename)
        if not filename.endswith(".py"):
            filename += ".py"
        if filename.startswith("_"):
            return False, "技能文件名不能以下划线开头"
        name = self._skill_name_from_file(filename)
        if self._origins.get(name) == ORIGIN_APP:
            return False, f"技能 {name} 与内置技能同名，主目录优先，拒绝覆盖"
        try:
            ast.parse(source_code)
        except SyntaxError as exc:
            return False, f"语法校验失败: {exc}"
        rejected = self._check_external_source(source_code)
        if rejected is not None:
            return False, rejected

        # Executable extensions are never accepted without a real test.
        if test_code is None or not test_code.strip():
            return False, "快车道技能必须附带非空 unittest 测试代码"
        rejected = self._check_test_source(test_code)
        if rejected is not None:
            return False, rejected
        rejected = self._run_forge_tests(
            name, filename, source_code, test_code, test_timeout
        )
        if rejected is not None:
            return False, rejected

        path = os.path.join(target_dir, filename)
        try:
            os.makedirs(target_dir, exist_ok=True)
            written = False
            try:
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(source_code)
                written = True
                module = self._load_module(path)
                self._validate_module(module)
                self._register_module(name, module)
                self._origins[name] = ORIGIN_APP_SPACE
            except Exception as exc:
                if written and os.path.exists(path):
                    os.remove(path)
                return False, f"技能注册失败: {exc}"
        except OSError as exc:
            return False, f"技能写入失败: {exc}"
        self._write_tested_marker(target_dir, name)
        self._audit_forge(f"name={name} origin=app-space tested=true")
        message = f"技能 {name} 注册成功（origin=app-space），已热加载可用"
        return True, f"{message}；已注册（含测试验证）；测试验证通过（tested=true）"

    def unregister_external_skill(self, name: str) -> tuple[bool, str]:
        """从注册表卸载一个快车道技能（不删文件）；内置技能拒绝。"""

        if name not in self.skills:
            return False, f"技能 {name} 未注册"
        if self._origins.get(name) != ORIGIN_APP_SPACE:
            return False, f"技能 {name} 是内置技能（origin=app），拒绝卸载"
        self.skills.pop(name, None)
        self._origins.pop(name, None)
        self.errors.pop(name, None)
        # 一并清理 tested 旁车标记（best-effort，不存在或无权限都忽略）
        if self.extra_skills_dirs:
            marker = self._tested_marker_path(self.extra_skills_dirs[0], name)
            try:
                if os.path.exists(marker):
                    os.remove(marker)
            except OSError:
                pass
        try:
            self._rebuild_tool_index()
        except Exception:
            self._tool_index = {}
        self._audit_forge(f"action=remove name={name} origin=app-space")
        return True, f"技能 {name} 已从注册表卸载"
