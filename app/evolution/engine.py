"""Agenelf 自我进化引擎。

基于 git 版本控制实现「自我修改核心代码」的完整闭环：
创建进化分支 → LLM 生成整文件修改 → 安全校验 → 应用修改 →
自动验证（pytest 优先，失败时降级为 compileall + 冒烟导入）→
成功则合并回 main，失败则回滚并删除进化分支。
全程步骤日志追加写入 evolution/evolution.log。

仅使用标准库，git 操作通过 subprocess 调用系统 git 完成。
"""

from __future__ import annotations

import datetime
import importlib.util
import os
import posixpath
import re
import subprocess
import sys


class EvolutionError(Exception):
    """进化流程中的可预期失败（携带中文失败原因）。"""


class EvolutionEngine:
    """自我进化引擎：让 Agent 在 git 保护下安全地修改自身代码。"""

    # 受保护文件（相对仓库根目录），LLM 一律禁止触碰
    PROTECTED_FILES = frozenset({"evolution/engine.py", "config.yaml"})
    # 受保护目录前缀，LLM 一律禁止触碰
    PROTECTED_DIRS = ("persona/",)
    # 单次进化允许修改的最大文件数
    MAX_FILES_PER_CHANGE = 3

    def __init__(self, repo_root: str):
        self.repo_root = os.path.abspath(repo_root)
        self.log_path = os.path.join(self.repo_root, "evolution", "evolution.log")

    # ------------------------------------------------------------------
    # 对外主流程
    # ------------------------------------------------------------------
    def propose_core_change(self, goal: str, llm) -> tuple[bool, str]:
        """执行一次自我修改流程，返回 (是否成功, 中文摘要/失败原因)。"""
        timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        branch = f"evolve/{timestamp}"
        self._log("开始进化流程", f"目标：{goal}")

        # 步骤 1：基于 main 创建进化分支
        ok, out = self._git("checkout", "main")
        if not ok:
            return self._fail_early(f"切换到 main 分支失败：{out}")
        ok, out = self._git("checkout", "-b", branch)
        if not ok:
            return self._fail_early(f"创建进化分支 {branch} 失败：{out}")
        self._log("步骤1 创建进化分支", branch)

        # applied 记录 (相对路径, 修改前是否已存在)，用于失败回滚
        applied: list[tuple[str, bool]] = []
        changes: dict[str, str] = {}
        try:
            # 步骤 2：调用 LLM，基于目标文件当前源码生成整文件修改
            source_files = self._collect_source_files()
            messages = self._build_messages(goal, source_files)
            response = llm.chat(messages, tools=None)
            content = (response or {}).get("content") or ""
            changes = self._parse_code_blocks(content)
            if not changes:
                raise EvolutionError("未能从 LLM 输出中解析出任何有效修改")
            self._log("步骤2 LLM 生成修改", f"涉及文件：{sorted(changes)}")

            # 步骤 3：安全校验（应用修改之前）
            self._validate_changes(changes)
            self._log("步骤3 安全校验", "通过")

            # 步骤 4 前半：应用修改（整文件覆盖写入）
            for rel_path, new_source in changes.items():
                abs_path = os.path.join(self.repo_root, rel_path)
                existed = os.path.exists(abs_path)
                os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                with open(abs_path, "w", encoding="utf-8") as fh:
                    fh.write(new_source)
                applied.append((rel_path, existed))
            self._log("步骤4 应用修改", f"已写入 {len(applied)} 个文件")

            # 步骤 4 后半：验证（pytest 优先，按可用性降级）
            passed, detail = self._verify(list(changes))
            if not passed:
                raise EvolutionError(f"验证失败：{detail}")
            self._log("步骤4 验证", detail)
        except Exception as exc:  # 包括 EvolutionError 与意外异常
            reason = str(exc)
            self._log("进化失败，执行回滚", reason)
            self._rollback(branch, applied)
            return (False, reason)

        # 步骤 5：提交进化分支 → 合并回 main → 删除进化分支
        ok, out = self._git("add", "--", *changes.keys())
        if not ok:
            self._log("进化失败，执行回滚", f"git add 失败：{out}")
            self._rollback(branch, applied)
            return (False, f"git add 失败：{out}")
        ok, out = self._git("commit", "-m", f"evolve: {goal[:60]}")
        if not ok:
            self._log("进化失败，执行回滚", f"git commit 失败：{out}")
            self._rollback(branch, applied)
            return (False, f"git commit 失败：{out}")
        ok, out = self._git("checkout", "main")
        if not ok:
            return self._fail_early(f"切回 main 失败：{out}（提交保留在分支 {branch}）")
        ok, out = self._git("merge", branch)
        if not ok:
            return self._fail_early(f"合并进化分支失败：{out}（提交保留在分支 {branch}）")
        self._git("branch", "-d", branch)

        summary = (
            f"进化成功：目标「{goal}」；修改文件 {sorted(changes)}；"
            f"分支 {branch} 已合并回 main 并删除"
        )
        self._log("步骤5 合并完成", summary)
        return (True, summary)

    # ------------------------------------------------------------------
    # 步骤 2：收集源码 + 构造 prompt + 解析 LLM 输出
    # ------------------------------------------------------------------
    def _collect_source_files(self) -> dict[str, str]:
        """收集仓库内可作为修改候选的 Python 源码（排除受保护路径）。"""
        collected: dict[str, str] = {}
        for dirpath, dirnames, filenames in os.walk(self.repo_root):
            # 跳过 git 元数据、缓存与隐藏目录
            dirnames[:] = [
                d for d in dirnames
                if d not in (".git", "__pycache__") and not d.startswith(".")
            ]
            for name in sorted(filenames):
                if not name.endswith(".py"):
                    continue
                abs_path = os.path.join(dirpath, name)
                rel_path = os.path.relpath(abs_path, self.repo_root)
                rel_path = rel_path.replace(os.sep, "/")
                if self._is_protected(rel_path):
                    continue
                try:
                    with open(abs_path, "r", encoding="utf-8") as fh:
                        collected[rel_path] = fh.read()
                except (OSError, UnicodeDecodeError):
                    continue
        return collected

    def _build_messages(self, goal: str, source_files: dict[str, str]) -> list[dict]:
        """构造发给 LLM 的消息：目标 + 全部候选文件当前源码 + 输出格式约定。"""
        file_sections = []
        for rel_path, source in sorted(source_files.items()):
            file_sections.append(
                f"### 文件：{rel_path}\n```python\n{source}\n```"
            )
        files_block = "\n\n".join(file_sections) if file_sections else "（仓库内暂无候选源码文件）"
        user_content = (
            "你是 Agenelf 的自我进化模块。请根据进化目标修改下面的项目源码。\n\n"
            f"【进化目标】\n{goal}\n\n"
            "【项目当前源码】\n"
            f"{files_block}\n\n"
            "【输出要求】\n"
            "1. 采用「整文件覆盖」模式：对每个需要修改的文件，输出它的完整新内容。\n"
            "2. 每个文件用一个 ```python 代码块包裹，代码块第一行必须是 "
            "`# FILE: <相对路径>`（从第二行开始才是文件内容）。\n"
            "3. 没有修改的文件不要输出；一次最多修改 "
            f"{self.MAX_FILES_PER_CHANGE} 个文件。\n"
            "4. 禁止修改：evolution/engine.py、config.yaml、persona/ 目录下的任何文件。\n"
            "5. 除代码块外只需简要说明，不要输出多余格式。"
        )
        return [
            {"role": "system", "content": "你是严谨的 Python 工程师，只输出可运行的代码。"},
            {"role": "user", "content": user_content},
        ]

    @staticmethod
    def _parse_code_blocks(content: str) -> dict[str, str]:
        """从 LLM 输出中解析 ```python 代码块，返回 {相对路径: 完整新内容}。

        约定每个代码块第一行为 `# FILE: <相对路径>`，其余行为文件内容。
        """
        changes: dict[str, str] = {}
        for match in re.finditer(r"```(?:python|py)\s*\n(.*?)```", content, re.DOTALL):
            block = match.group(1)
            lines = block.splitlines()
            if not lines:
                continue
            head = re.match(r"#\s*FILE\s*[:：]\s*(\S+)", lines[0].strip(), re.IGNORECASE)
            if not head:
                continue
            rel_path = head.group(1)
            body = "\n".join(lines[1:])
            if not body.endswith("\n"):
                body += "\n"
            changes[rel_path] = body
        return changes

    # ------------------------------------------------------------------
    # 步骤 3：安全校验
    # ------------------------------------------------------------------
    def _is_protected(self, rel_path: str) -> bool:
        """判断相对路径是否属于禁止修改的范围。"""
        if rel_path in self.PROTECTED_FILES:
            return True
        return any(rel_path.startswith(prefix) for prefix in self.PROTECTED_DIRS)

    def _validate_changes(self, changes: dict[str, str]) -> None:
        """应用修改前的安全校验，违规时抛出 EvolutionError。"""
        if len(changes) > self.MAX_FILES_PER_CHANGE:
            raise EvolutionError(
                f"单次改动文件数 {len(changes)} 超过上限 {self.MAX_FILES_PER_CHANGE}"
            )
        for raw_path in changes:
            norm = posixpath.normpath(raw_path.replace("\\", "/"))
            if posixpath.isabs(norm) or norm == ".." or norm.startswith("../"):
                raise EvolutionError(f"非法路径（疑似越出仓库根目录）：{raw_path}")
            if self._is_protected(norm):
                raise EvolutionError(f"禁止修改受保护路径：{norm}")

    # ------------------------------------------------------------------
    # 步骤 4：验证（pytest 优先，按可用性降级）
    # ------------------------------------------------------------------
    def _verify(self, changed_files: list[str]) -> tuple[bool, str]:
        """验证修改后的代码。返回 (是否通过, 验证方式说明/失败详情)。"""
        tests_dir = os.path.join(self.repo_root, "tests")
        pytest_available = importlib.util.find_spec("pytest") is not None
        if pytest_available and os.path.isdir(tests_dir):
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "tests/", "-x", "-q"],
                cwd=self.repo_root, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
            )
            if proc.returncode == 0:
                return (True, "pytest 全部通过")
            tail = (proc.stdout + proc.stderr).strip().splitlines()[-5:]
            return (False, "pytest 失败：" + " | ".join(tail))

        # 降级方案：compileall 编译检查 + 对改动模块做冒烟 import
        compile_dirs = [
            d for d in ("core", "evolution")
            if os.path.isdir(os.path.join(self.repo_root, d))
        ]
        if compile_dirs:
            proc = subprocess.run(
                [sys.executable, "-m", "compileall", "-q", *compile_dirs],
                cwd=self.repo_root, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
            )
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout).strip().splitlines()
                return (False, "compileall 失败：" + " | ".join(detail[-3:]))

        for rel_path in changed_files:
            module = self._module_name_for(rel_path)
            if module is not None:
                proc = subprocess.run(
                    [sys.executable, "-c", f"import {module}"],
                    cwd=self.repo_root, capture_output=True, text=True,
                    encoding="utf-8", errors="replace",
                )
                if proc.returncode != 0:
                    detail = proc.stderr.strip().splitlines()
                    return (False, f"冒烟导入 {module} 失败：" + " | ".join(detail[-3:]))
            else:
                # 不在包内的文件退化为单文件语法编译检查
                proc = subprocess.run(
                    [sys.executable, "-m", "py_compile", rel_path],
                    cwd=self.repo_root, capture_output=True, text=True,
                    encoding="utf-8", errors="replace",
                )
                if proc.returncode != 0:
                    detail = (proc.stderr or proc.stdout).strip().splitlines()
                    return (False, f"语法编译 {rel_path} 失败：" + " | ".join(detail[-3:]))
        return (True, "compileall + 冒烟导入通过（pytest 不可用，已降级）")

    def _module_name_for(self, rel_path: str) -> str | None:
        """把仓库内相对路径转换为可导入的模块名；不在包内则返回 None。"""
        if not rel_path.endswith(".py"):
            return None
        parts = rel_path[:-3].split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
            if not parts:
                return None
        # 逐级确认所在目录是 Python 包（含 __init__.py）
        dir_parts = parts[:-1]
        for i in range(1, len(dir_parts) + 1):
            init_file = os.path.join(self.repo_root, *dir_parts[:i], "__init__.py")
            if not os.path.isfile(init_file):
                return None
        return ".".join(parts)

    # ------------------------------------------------------------------
    # 失败回滚与日志
    # ------------------------------------------------------------------
    def _rollback(self, branch: str, applied: list[tuple[str, bool]]) -> None:
        """恢复工作区：还原被修改文件、删除新建文件、切回 main 并删除进化分支。"""
        for rel_path, existed in reversed(applied):
            if existed:
                self._git("checkout", "main", "--", rel_path)
            else:
                try:
                    os.remove(os.path.join(self.repo_root, rel_path))
                except OSError:
                    pass
        self._git("checkout", "main")
        self._git("branch", "-D", branch)
        self._log("回滚完成", f"已切回 main，进化分支 {branch} 已删除，工作区恢复干净")

    def _fail_early(self, reason: str) -> tuple[bool, str]:
        """尚未产生工作区改动时的失败出口（无需回滚）。"""
        self._log("进化失败", reason)
        return (False, reason)

    def _log(self, step: str, result: str) -> None:
        """向 evolution/evolution.log 追加一行步骤日志（时间戳 + 步骤 + 结果）。"""
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] {step} | {result}\n"
        try:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            # 日志写入失败不应阻断进化主流程
            pass

    def _git(self, *args: str) -> tuple[bool, str]:
        """执行 git 命令，返回 (是否成功, 标准输出/错误摘要)。"""
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            return (False, f"无法执行 git：{exc}")
        output = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return (proc.returncode == 0, output)
