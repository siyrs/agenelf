"""EvolutionEngine 的单元测试。

在临时目录中初始化独立 git 仓库（模拟项目结构：core/dummy.py + 简单测试文件），
通过 FakeLLM 覆盖三个场景：成功路径、失败回滚路径、安全约束拒绝路径。
不触碰真实项目仓库。

兼容两种运行方式：
    python -m unittest tests.test_evolution
    python tests/test_evolution.py
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# 保证无论从哪个目录运行都能导入被测的 evolution 包
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evolution.engine import EvolutionEngine  # noqa: E402


# ----------------------------------------------------------------------
# 测试替身：符合契约的 FakeLLM
# ----------------------------------------------------------------------
class FakeLLM:
    """模拟 LLM：chat() 返回契约格式 {"content": str|None, "tool_calls": [...]}。"""

    def __init__(self, response_content: str):
        self.response_content = response_content
        self.calls = []  # 记录每次调用的 (messages, tools)，便于断言

    def chat(self, messages, tools=None):
        self.calls.append((messages, tools))
        return {"content": self.response_content, "tool_calls": []}


# ----------------------------------------------------------------------
# 临时 git 仓库脚手架
# ----------------------------------------------------------------------
DUMMY_SOURCE = 'def greet(name):\n    return f"你好，{name}"\n'

DUMMY_TEST_SOURCE = '''import unittest

from core.dummy import greet


class DummyTest(unittest.TestCase):
    def test_greet_包含名字(self):
        self.assertIn("世界", greet("世界"))


if __name__ == "__main__":
    unittest.main()
'''

GITIGNORE_SOURCE = "__pycache__/\n*.pyc\nevolution/evolution.log\n"


def make_code_block(rel_path: str, source: str) -> str:
    """按引擎约定构造带 # FILE: 标记的 ```python 代码块。"""
    return f"```python\n# FILE: {rel_path}\n{source}```\n"


class EvolutionEngineTestCase(unittest.TestCase):
    """每个用例都在独立的临时 git 仓库中运行。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)

        # 初始化仓库：main 分支 + 仓库级提交身份
        self._git("init", "-b", "main")
        self._git("config", "user.name", "进化测试员")
        self._git("config", "user.email", "evolution-test@example.com")

        # 模拟项目结构：core/dummy.py + 一个简单测试文件
        (self.repo / "core").mkdir()
        (self.repo / "tests").mkdir()
        (self.repo / "core" / "__init__.py").write_text("", encoding="utf-8")
        (self.repo / "core" / "dummy.py").write_text(DUMMY_SOURCE, encoding="utf-8")
        (self.repo / "tests" / "test_dummy.py").write_text(DUMMY_TEST_SOURCE, encoding="utf-8")
        (self.repo / ".gitignore").write_text(GITIGNORE_SOURCE, encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-m", "init: 模拟项目结构")

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------
    def _git(self, *args: str) -> subprocess.CompletedProcess:
        """在临时仓库中执行 git，失败立即让测试报错。"""
        proc = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode != 0:
            self.fail(f"git {' '.join(args)} 失败：{proc.stderr.strip()}")
        return proc

    def _git_output(self, *args: str) -> str:
        return self._git(*args).stdout.strip()

    def _read(self, rel_path: str) -> str:
        return (self.repo / rel_path).read_text(encoding="utf-8")

    def _assert_workspace_clean(self):
        """断言工作区干净（无未提交改动、无未跟踪文件）。"""
        status = self._git_output("status", "--porcelain")
        self.assertEqual(status, "", f"工作区应恢复干净，实际：{status!r}")

    def _assert_no_evolve_branch(self):
        branches = self._git_output("branch", "--list", "evolve/*")
        self.assertEqual(branches, "", f"不应残留进化分支，实际：{branches!r}")

    # ------------------------------------------------------------------
    # 场景 1：成功路径
    # ------------------------------------------------------------------
    def test_成功路径_修改合并回main(self):
        new_source = 'def greet(name):\n    return f"你好，{name}！"\n'
        response = (
            "好的，下面是修改后的完整文件：\n"
            + make_code_block("core/dummy.py", new_source)
        )
        llm = FakeLLM(response)

        engine = EvolutionEngine(str(self.repo))
        ok, summary = engine.propose_core_change("给 dummy.py 的 greet 函数加感叹号", llm)

        self.assertTrue(ok, f"进化应成功，实际返回：{summary}")
        # LLM 按契约被调用：messages + tools=None
        self.assertEqual(len(llm.calls), 1)
        messages, tools = llm.calls[0]
        self.assertIsNone(tools)
        self.assertTrue(any("core/dummy.py" in m["content"] for m in messages))
        # main 分支上的文件已包含新代码
        self.assertEqual(self._git_output("rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertEqual(self._read("core/dummy.py"), new_source)
        self.assertIn("！", self._read("core/dummy.py"))
        # 进化提交已进入 main 历史，进化分支已删除
        self.assertIn("evolve:", self._git_output("log", "--oneline"))
        self._assert_no_evolve_branch()
        self._assert_workspace_clean()
        # 步骤日志已写入
        log_text = (self.repo / "evolution" / "evolution.log").read_text(encoding="utf-8")
        self.assertIn("开始进化流程", log_text)
        self.assertIn("进化成功", log_text)

    # ------------------------------------------------------------------
    # 场景 2：失败回滚路径（LLM 返回语法错误的代码）
    # ------------------------------------------------------------------
    def test_失败路径_语法错误触发回滚(self):
        bad_source = 'def greet(name)\n    return f"你好，{name}！"\n'  # 缺少冒号
        response = "修改如下：\n" + make_code_block("core/dummy.py", bad_source)
        llm = FakeLLM(response)

        engine = EvolutionEngine(str(self.repo))
        ok, reason = engine.propose_core_change("给 dummy.py 的 greet 函数加感叹号", llm)

        self.assertFalse(ok, "语法错误的修改应被拒绝")
        self.assertIn("验证失败", reason)
        # main 分支代码未被污染
        self.assertEqual(self._git_output("rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertEqual(self._read("core/dummy.py"), DUMMY_SOURCE)
        # 工作区干净、无残留进化分支、main 历史上没有进化提交
        self._assert_workspace_clean()
        self._assert_no_evolve_branch()
        self.assertNotIn("evolve:", self._git_output("log", "--oneline"))
        # 日志记录了失败与回滚
        log_text = (self.repo / "evolution" / "evolution.log").read_text(encoding="utf-8")
        self.assertIn("进化失败", log_text)
        self.assertIn("回滚完成", log_text)

    # ------------------------------------------------------------------
    # 场景 3：安全约束（LLM 试图修改 evolution/engine.py）
    # ------------------------------------------------------------------
    def test_安全约束_禁止修改engine自身(self):
        malicious = "# 恶意覆盖\nraise SystemExit('boom')\n"
        response = (
            "我来直接重写进化引擎：\n"
            + make_code_block("evolution/engine.py", malicious)
        )
        llm = FakeLLM(response)

        engine = EvolutionEngine(str(self.repo))
        ok, reason = engine.propose_core_change("重写进化引擎", llm)

        self.assertFalse(ok, "修改受保护文件必须被拒绝")
        self.assertIn("禁止修改受保护路径", reason)
        self.assertIn("evolution/engine.py", reason)
        # 受保护文件没有被创建/污染，工作区与分支状态完好
        self.assertFalse((self.repo / "evolution" / "engine.py").exists())
        self.assertEqual(self._read("core/dummy.py"), DUMMY_SOURCE)
        self.assertEqual(self._git_output("rev-parse", "--abbrev-ref", "HEAD"), "main")
        self._assert_workspace_clean()
        self._assert_no_evolve_branch()


if __name__ == "__main__":
    unittest.main(verbosity=2)
