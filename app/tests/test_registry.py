"""技能协议与功能测试。

按技能协议对 skills/ 下的 4 个内置技能做 duck-type 校验
（SKILL_META / TOOLS / execute，且 TOOLS 中每个函数名都能被 execute 路由），
并对关键路径做实测：
- code_writer：write_code_file 写打印脚本 + run_python 运行；
- server_ops：白名单命令直接执行、非白名单命令需确认、端口检测、磁盘状态；
- task_handler：笔记保存/读取往返、待办落盘。

所有落盘均在临时目录中进行，不污染项目。
兼容两种运行方式：``pytest tests/test_registry.py`` 或 ``python tests/test_registry.py``。
"""

from __future__ import annotations

import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

# 保证 python tests/test_registry.py 直接运行时也能 import skills 包
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SKILL_MODULES = ["code_writer", "ai_tools", "server_ops", "task_handler"]


def _load_skills() -> dict:
    """以协议方式（duck-type）加载全部技能模块。"""
    return {name: importlib.import_module(f"skills.{name}") for name in SKILL_MODULES}


class TestSkillProtocol(unittest.TestCase):
    """协议符合性：SKILL_META / TOOLS / execute 与路由完整性。"""

    @classmethod
    def setUpClass(cls):
        cls.skills = _load_skills()

    def test_all_skills_importable(self):
        self.assertEqual(set(self.skills), set(SKILL_MODULES))

    def test_skill_meta_shape(self):
        for name, mod in self.skills.items():
            with self.subTest(skill=name):
                meta = getattr(mod, "SKILL_META", None)
                self.assertIsInstance(meta, dict, "缺少 SKILL_META")
                # 协议：name 必须与文件名一致
                self.assertEqual(meta.get("name"), name)
                self.assertIsInstance(meta.get("description"), str)
                self.assertTrue(meta["description"], "description 不能为空")
                self.assertIsInstance(meta.get("version"), str)

    def test_tools_schema(self):
        for name, mod in self.skills.items():
            with self.subTest(skill=name):
                tools = getattr(mod, "TOOLS", None)
                self.assertIsInstance(tools, list)
                self.assertTrue(tools, "TOOLS 不能为空")
                for tool in tools:
                    self.assertEqual(tool.get("type"), "function")
                    fn = tool.get("function") or {}
                    self.assertIsInstance(fn.get("name"), str)
                    self.assertIsInstance(fn.get("description"), str)
                    params = fn.get("parameters") or {}
                    self.assertEqual(params.get("type"), "object")
                    self.assertIn("properties", params)
                    self.assertIn("required", params)

    def test_every_tool_routable_by_execute(self):
        for name, mod in self.skills.items():
            execute = getattr(mod, "execute", None)
            self.assertTrue(callable(execute), f"{name} 缺少 execute")
            for tool in mod.TOOLS:
                tool_name = tool["function"]["name"]
                with self.subTest(skill=name, tool=tool_name):
                    # 空参调用也应被路由（返回参数错误而非"未知工具"）
                    result = execute(tool_name, {})
                    self.assertIsInstance(result, str)
                    self.assertFalse(
                        result.startswith("未知工具"),
                        f"execute 未能路由 {tool_name}",
                    )

    def test_unknown_tool_returns_error(self):
        for name, mod in self.skills.items():
            with self.subTest(skill=name):
                result = mod.execute("no_such_tool", {})
                self.assertIn("未知工具", result)


class TestCodeWriter(unittest.TestCase):
    """code_writer：写文件限制 + 运行片段。"""

    def setUp(self):
        self.skills = _load_skills()["code_writer"]
        self.tmp = tempfile.TemporaryDirectory()
        self.skills.set_project_root(self.tmp.name)

    def tearDown(self):
        self.skills.set_project_root(None)
        self.tmp.cleanup()

    def test_write_and_run_print_script(self):
        # 写一个打印脚本，并用 run_python 实际运行它
        path_str = self.skills.execute(
            "write_code_file",
            {"path": "scripts/hello.py", "content": "print('你好，Agenelf')"},
        )
        written = Path(path_str)
        self.assertTrue(written.is_file(), f"文件未写入：{path_str}")
        self.assertTrue(written.is_relative_to(Path(self.tmp.name)))

        result = self.skills.execute(
            "run_python",
            {"code": f"import runpy; runpy.run_path({str(written)!r})"},
        )
        self.assertIn("退出码：0", result)
        self.assertIn("你好，Agenelf", result)

    def test_run_python_captures_stderr(self):
        result = self.skills.execute(
            "run_python", {"code": "import sys; sys.stderr.write('oops')"}
        )
        self.assertIn("oops", result)

    def test_reject_dotdot_escape(self):
        result = self.skills.execute(
            "write_code_file", {"path": "../evil.py", "content": "x = 1"}
        )
        self.assertIn("逃逸", result)
        self.assertFalse((Path(self.tmp.name).parent / "evil.py").exists())

    def test_reject_absolute_escape(self):
        result = self.skills.execute(
            "write_code_file", {"path": "/tmp/evil_abs.py", "content": "x = 1"}
        )
        self.assertIn("逃逸", result)
        self.assertFalse(Path("/tmp/evil_abs.py").exists())


class TestAiTools(unittest.TestCase):
    """ai_tools：mock 模式与注入 LLM 两种路径。"""

    def setUp(self):
        self.skills = _load_skills()["ai_tools"]

    def tearDown(self):
        self.skills.set_llm(None)

    def test_mock_mode_when_llm_not_set(self):
        self.skills.set_llm(None)
        self.assertIn("mock", self.skills.execute("ask_llm", {"prompt": "hi"}))
        self.assertIn("mock", self.skills.execute("summarize", {"text": "长文本"}))

    def test_injected_llm_receives_messages(self):
        seen = []

        def fake_llm(messages):  # 符合 fn(messages: list[dict]) -> str 协议
            seen.append(messages)
            return "模型回答"

        self.skills.set_llm(fake_llm)
        result = self.skills.execute(
            "ask_llm", {"prompt": "问题", "system": "你是助手"}
        )
        self.assertEqual(result, "模型回答")
        self.assertEqual(
            seen[0],
            [
                {"role": "system", "content": "你是助手"},
                {"role": "user", "content": "问题"},
            ],
        )


class TestServerOps(unittest.TestCase):
    """server_ops：白名单放行、确认闸门、端口与磁盘检测。"""

    def setUp(self):
        self.skills = _load_skills()["server_ops"]

    def test_whitelist_command_runs_directly(self):
        result = self.skills.execute("run_shell", {"command": "uname"})
        self.assertIn("退出码：0", result)
        self.assertTrue("Linux" in result or "MINGW" in result)

    def test_non_whitelist_requires_confirm(self):
        result = self.skills.execute("run_shell", {"command": "echo hi"})
        self.assertIn("确认", result)
        # confirm=True 后放行
        result_ok = self.skills.execute(
            "run_shell", {"command": "echo hi", "confirm": True}
        )
        self.assertIn("退出码：0", result_ok)
        self.assertIn("hi", result_ok)

    def test_check_service_closed_port(self):
        result = self.skills.execute(
            "check_service", {"host": "127.0.0.1", "port": 1}
        )
        self.assertIn("不通", result)

    def test_disk_status(self):
        result = self.skills.execute("disk_status", {})
        self.assertIn("Filesystem", result)


class TestTaskHandler(unittest.TestCase):
    """task_handler：笔记存取往返与待办落盘（临时目录）。"""

    def setUp(self):
        self.skills = _load_skills()["task_handler"]
        self.tmp = tempfile.TemporaryDirectory()
        self.skills.set_store_dir(self.tmp.name)

    def tearDown(self):
        self.skills.set_store_dir(None)
        self.tmp.cleanup()

    def test_note_roundtrip(self):
        save_result = self.skills.execute(
            "save_note", {"title": "部署备忘", "content": "先备份再发布"}
        )
        self.assertIn("已保存", save_result)
        content = self.skills.execute("read_note", {"title": "部署备忘"})
        self.assertEqual(content, "先备份再发布")

    def test_read_missing_note(self):
        result = self.skills.execute("read_note", {"title": "不存在"})
        self.assertIn("不存在", result)

    def test_create_todo_persists_json(self):
        result = self.skills.execute(
            "create_todo", {"items": ["写技能", "跑测试"]}
        )
        self.assertIn("已创建 2 条待办", result)
        data = json.loads(
            (Path(self.tmp.name) / "todos.json").read_text(encoding="utf-8")
        )
        self.assertEqual([t["item"] for t in data["todos"]], ["写技能", "跑测试"])

    def test_title_sanitized_against_traversal(self):
        # 标题中的路径穿越字符不应逃逸出 notes/ 目录
        self.skills.execute(
            "save_note", {"title": "../../evil", "content": "x"}
        )
        store = Path(self.tmp.name)
        self.assertFalse((store.parent / "evil.txt").exists())
        self.assertTrue(list((store / "notes").glob("*.txt")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
