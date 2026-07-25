"""skill_forge 技能全流程测试：forge → list → dispatch → remove 及安全拒绝。"""

from __future__ import annotations

import json
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path

from core.registry import SkillRegistry

_BUILTIN_SOURCE = '''
SKILL_META = {"name": "builtin_ops", "description": "内置", "version": "1.0"}
TOOLS = [{"type": "function", "function": {"name": "builtin_tool", "description": "x", "parameters": {"type": "object", "properties": {}, "required": []}}}]
def execute(tool_name, args):
    return "builtin"
'''

_FORGE_SOURCE = '''
SKILL_META = {"name": "hello_echo", "description": "回声技能", "version": "0.1.0"}
TOOLS = [{"type": "function", "function": {"name": "hello_echo", "description": "回声", "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": []}}}]
def execute(tool_name, args):
    return "echo:" + str((args or {}).get("text", ""))
'''


class _FakeAgent:
    """最小 Agent 替身：只提供 skill_forge 会用到的刷新钩子。"""

    def __init__(self) -> None:
        self.refreshed = 0
        self.configured: list[str] = []

    def configure_skill_runtimes(self, name: str) -> None:
        self.configured.append(name)

    def _refresh_system_prompt(self) -> None:
        self.refreshed += 1


class SkillForgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.main_dir = self.root / "app" / "skills"
        self.appspace_dir = self.root / "app-space" / "skills"
        self.main_dir.mkdir(parents=True)
        self.appspace_dir.mkdir(parents=True)
        # 主目录：真实 skill_forge + 一个内置技能
        real_forge = (
            Path(__file__).resolve().parents[1] / "skills" / "skill_forge.py"
        )
        shutil.copy(real_forge, self.main_dir / "skill_forge.py")
        (self.main_dir / "builtin_ops.py").write_text(
            textwrap.dedent(_BUILTIN_SOURCE), encoding="utf-8"
        )
        self.registry = SkillRegistry(
            str(self.main_dir), extra_skills_dirs=[str(self.appspace_dir)]
        )
        self.registry.discover()
        self.agent = _FakeAgent()
        forge_module = self.registry.skills["skill_forge"]
        forge_module.configure_runtime(agent=self.agent, registry=self.registry)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _forge(self, name: str, source: str, description: str = "测试技能") -> dict:
        result = self.registry.dispatch(
            "forge_skill",
            {"name": name, "description": description, "source_code": source},
        )
        return json.loads(result)

    def test_forge_list_dispatch_remove_full_flow(self):
        forged = self._forge("hello_echo", textwrap.dedent(_FORGE_SOURCE))
        self.assertTrue(forged["forged"], forged)
        self.assertEqual(forged["origin"], "app-space")
        self.assertIn("热加载", forged["message"])
        self.assertIn("hello_echo", self.agent.configured)

        listed = json.loads(self.registry.dispatch("list_forged_skills", {}))
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["skills"][0]["name"], "hello_echo")
        self.assertEqual(listed["skills"][0]["version"], "0.1.0")
        self.assertEqual(listed["skills"][0]["description"], "回声技能")

        # 热加载后即可 dispatch 调用新技能
        output = self.registry.dispatch("hello_echo", {"text": "你好"})
        self.assertEqual(output, "echo:你好")

        removed = json.loads(
            self.registry.dispatch("remove_forged_skill", {"name": "hello_echo"})
        )
        self.assertTrue(removed["removed"], removed)
        self.assertFalse((self.appspace_dir / "hello_echo.py").exists())
        self.assertIn("未知工具", self.registry.dispatch("hello_echo", {}))
        listed = json.loads(self.registry.dispatch("list_forged_skills", {}))
        self.assertEqual(listed["count"], 0)

    def test_forge_rejects_same_name_as_builtin(self):
        rejected = self._forge(
            "builtin_ops",
            textwrap.dedent(_FORGE_SOURCE).replace("hello_echo", "builtin_ops"),
        )
        self.assertFalse(rejected["forged"])
        self.assertIn("同名", rejected["message"])
        self.assertFalse((self.appspace_dir / "builtin_ops.py").exists())

    def test_forge_rejects_invalid_and_protected_names(self):
        bad = self._forge("Bad-Name", textwrap.dedent(_FORGE_SOURCE))
        self.assertFalse(bad["forged"])
        self.assertIn("小写字母", bad["message"])
        protected = self._forge("registry", textwrap.dedent(_FORGE_SOURCE))
        self.assertFalse(protected["forged"])
        self.assertIn("保护清单", protected["message"])

    def test_forge_rejects_bad_source(self):
        rejected = self._forge("broken_skill", "def broken(:\n")
        self.assertFalse(rejected["forged"])
        self.assertIn("语法校验失败", rejected["message"])
        self.assertFalse((self.appspace_dir / "broken_skill.py").exists())

    def test_remove_rejects_builtin_skill(self):
        result = json.loads(
            self.registry.dispatch("remove_forged_skill", {"name": "builtin_ops"})
        )
        self.assertFalse(result["removed"])
        self.assertIn("内置技能", result["message"])
        # 内置技能仍在且可调用
        self.assertEqual(self.registry.dispatch("builtin_tool", {}), "builtin")

    def test_remove_rejects_unknown_skill(self):
        result = json.loads(
            self.registry.dispatch("remove_forged_skill", {"name": "ghost"})
        )
        self.assertFalse(result["removed"])
        self.assertIn("不在 app-space", result["message"])

    def test_audit_log_records_forge_and_remove(self):
        self._forge("hello_echo", textwrap.dedent(_FORGE_SOURCE))
        self.registry.dispatch("remove_forged_skill", {"name": "hello_echo"})
        log_path = self.root / "logs" / "audit.log"
        self.assertTrue(log_path.is_file())
        content = log_path.read_text(encoding="utf-8")
        self.assertIn("name=hello_echo", content)
        self.assertIn("origin=app-space", content)
        self.assertIn("action=remove", content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
