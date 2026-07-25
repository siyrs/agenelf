"""app-space 快车道注册表测试：双目录发现、主目录优先与外部注册校验。"""

from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from core.registry import SkillRegistry

_SKILL_TEMPLATE = '''
SKILL_META = {{"name": "{name}", "description": "{desc}", "version": "1.0"}}
TOOLS = [{{"type": "function", "function": {{"name": "{tool}", "description": "x", "parameters": {{"type": "object", "properties": {{}}, "required": []}}}}}}]
def execute(tool_name, args):
    return "{marker}"
'''


def _skill_source(name: str, tool: str, marker: str, desc: str = "x") -> str:
    return textwrap.dedent(
        _SKILL_TEMPLATE.format(name=name, tool=tool, marker=marker, desc=desc)
    )


def _test_source(name: str, tool: str, expected: str) -> str:
    cls = "".join(part.title() for part in name.split("_"))
    return textwrap.dedent(
        f"""
        import unittest
        import {name}

        class {cls}Test(unittest.TestCase):
            def test_tool(self):
                self.assertEqual({name}.execute({tool!r}, {{}}), {expected!r})
        """
    )


class AppSpaceRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        # 运行根布局：app/skills（主目录，慢车道）+ app-space/skills（快车道）
        self.main_dir = self.root / "app" / "skills"
        self.appspace_dir = self.root / "app-space" / "skills"
        self.main_dir.mkdir(parents=True)
        self.appspace_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, directory: Path, filename: str, source: str) -> None:
        (directory / filename).write_text(source, encoding="utf-8")

    def _registry(self) -> SkillRegistry:
        return SkillRegistry(
            str(self.main_dir), extra_skills_dirs=[str(self.appspace_dir)]
        )

    def test_extra_dir_skill_is_discovered_and_dispatched(self):
        self._write(
            self.appspace_dir,
            "forged_echo.py",
            _skill_source("forged_echo", "echo_tool", "from-appspace"),
        )
        registry = self._registry()
        discovered = registry.discover()
        self.assertIn("forged_echo", discovered)
        self.assertEqual(registry.origin_of("forged_echo"), "app-space")
        self.assertEqual(registry.dispatch("echo_tool", {}), "from-appspace")
        catalog = registry.capability_catalog()
        origins = {item["source_skill"]: item["origin"] for item in catalog}
        self.assertEqual(origins["forged_echo"], "app-space")

    def test_main_dir_wins_on_same_name(self):
        self._write(
            self.main_dir,
            "dual.py",
            _skill_source("dual", "dual_tool", "from-main"),
        )
        self._write(
            self.appspace_dir,
            "dual.py",
            _skill_source("dual", "dual_tool", "from-appspace"),
        )
        registry = self._registry()
        registry.discover()
        self.assertEqual(registry.dispatch("dual_tool", {}), "from-main")
        self.assertEqual(registry.origin_of("dual"), "app")
        catalog = registry.capability_catalog()
        origins = {item["source_skill"]: item["origin"] for item in catalog}
        self.assertEqual(origins["dual"], "app")

    def test_register_external_skill_success(self):
        registry = self._registry()
        registry.discover()
        ok, message = registry.register_external_skill(
            str(self.appspace_dir),
            "hot_skill.py",
            _skill_source("hot_skill", "hot_tool", "hot-loaded"),
            test_code=_test_source("hot_skill", "hot_tool", "hot-loaded"),
        )
        self.assertTrue(ok, message)
        self.assertIn("app-space", message)
        self.assertEqual(registry.dispatch("hot_tool", {}), "hot-loaded")
        self.assertTrue((self.appspace_dir / "hot_skill.py").is_file())
        self.assertEqual(registry.origin_of("hot_skill"), "app-space")

    def test_register_external_skill_requires_tests(self):
        registry = self._registry()
        registry.discover()
        ok, message = registry.register_external_skill(
            str(self.appspace_dir),
            "untested.py",
            _skill_source("untested", "untested_tool", "x"),
        )
        self.assertFalse(ok)
        self.assertIn("必须附带", message)
        self.assertFalse((self.appspace_dir / "untested.py").exists())

    def test_register_external_skill_syntax_error_leaves_no_file(self):
        registry = self._registry()
        registry.discover()
        ok, message = registry.register_external_skill(
            str(self.appspace_dir), "bad.py", "def broken(:\n"
        )
        self.assertFalse(ok)
        self.assertIn("语法校验失败", message)
        self.assertFalse((self.appspace_dir / "bad.py").exists())
        self.assertNotIn("bad", registry.skills)

    def test_register_external_skill_protocol_error_leaves_no_file(self):
        registry = self._registry()
        registry.discover()
        ok, message = registry.register_external_skill(
            str(self.appspace_dir),
            "no_proto.py",
            "SKILL_META = {}\n",  # 缺少 TOOLS 与 execute
        )
        self.assertFalse(ok)
        self.assertIn("必须附带", message)
        self.assertFalse((self.appspace_dir / "no_proto.py").exists())
        self.assertNotIn("no_proto", registry.skills)

    def test_register_external_skill_rejects_unauthorized_dir(self):
        registry = self._registry()
        registry.discover()
        ok, message = registry.register_external_skill(
            str(self.main_dir),  # 越权：只允许写 app-space/skills
            "sneaky.py",
            _skill_source("sneaky", "sneaky_tool", "x"),
        )
        self.assertFalse(ok)
        self.assertIn("越权路径", message)
        self.assertFalse((self.main_dir / "sneaky.py").exists())

    def test_register_external_skill_rejects_builtin_same_name(self):
        self._write(
            self.main_dir,
            "builtin.py",
            _skill_source("builtin", "builtin_tool", "from-main"),
        )
        registry = self._registry()
        registry.discover()
        ok, message = registry.register_external_skill(
            str(self.appspace_dir),
            "builtin.py",
            _skill_source("builtin", "other_tool", "from-appspace"),
        )
        self.assertFalse(ok)
        self.assertIn("同名", message)
        self.assertEqual(registry.dispatch("builtin_tool", {}), "from-main")

    def test_register_external_skill_audits_success(self):
        registry = self._registry()
        registry.discover()
        ok, _ = registry.register_external_skill(
            str(self.appspace_dir),
            "audited.py",
            _skill_source("audited", "audited_tool", "x"),
            test_code=_test_source("audited", "audited_tool", "x"),
        )
        self.assertTrue(ok)
        log_path = self.root / "logs" / "audit.log"
        self.assertTrue(log_path.is_file())
        content = log_path.read_text(encoding="utf-8")
        self.assertIn("[skill_forge]", content)
        self.assertIn("name=audited", content)
        self.assertIn("origin=app-space", content)

    def test_unregister_external_skill_only_for_appspace(self):
        self._write(
            self.main_dir,
            "builtin.py",
            _skill_source("builtin", "builtin_tool", "from-main"),
        )
        registry = self._registry()
        registry.discover()
        ok, message = registry.unregister_external_skill("builtin")
        self.assertFalse(ok)
        self.assertIn("内置技能", message)
        ok, _ = registry.register_external_skill(
            self.appspace_dir,
            "temp_skill.py",
            _skill_source("temp_skill", "temp_tool", "x"),
            test_code=_test_source("temp_skill", "temp_tool", "x"),
        )
        self.assertTrue(ok)
        ok, _ = registry.unregister_external_skill("temp_skill")
        self.assertTrue(ok)
        self.assertIn("未知工具", registry.dispatch("temp_tool", {}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
