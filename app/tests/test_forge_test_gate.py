"""快车道测试门禁测试：forge 附测试沙盒验证、拒绝行为与 tested 标注。

覆盖矩阵：
- 附通过测试 → 注册成功且 tested 标记存在
- 附失败测试 → 拒绝注册且技能文件不存在
- 测试语法错误 / 规模超限 → 拒绝
- 测试超时（缩短 test_timeout）→ 拒绝
- 未附测试 → 注册成功但标注未测试
- forge/list/remove 全链路的 tested 标注与标记清理
"""

from __future__ import annotations

import json
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path

from core.registry import SkillRegistry

_SKILL_SOURCE = '''
SKILL_META = {{"name": "{name}", "description": "回声技能", "version": "0.1.0"}}
TOOLS = [{{"type": "function", "function": {{"name": "{tool}", "description": "回声", "parameters": {{"type": "object", "properties": {{"text": {{"type": "string"}}}}, "required": []}}}}}}]
def execute(tool_name, args):
    return "echo:" + str((args or {{}}).get("text", ""))
'''

# 通过的测试：import 技能模块并断言 execute 行为
_PASSING_TEST = '''
import unittest

import {name}


class {cls}Test(unittest.TestCase):
    def test_execute_echoes_text(self):
        self.assertEqual({name}.execute("{tool}", {{"text": "你好"}}), "echo:你好")

    def test_execute_empty_args(self):
        self.assertEqual({name}.execute("{tool}", {{}}), "echo:")


if __name__ == "__main__":
    unittest.main()
'''

# 失败的测试：断言与技能实际行为相反
_FAILING_TEST = '''
import unittest

import {name}


class {cls}Test(unittest.TestCase):
    def test_wrong_expectation(self):
        self.assertEqual({name}.execute("{tool}", {{"text": "x"}}), "WRONG")


if __name__ == "__main__":
    unittest.main()
'''

# 超时测试：sleep 远超缩短后的 test_timeout
_SLOW_TEST = '''
import time
import unittest

import {name}


class {cls}Test(unittest.TestCase):
    def test_sleep_forever(self):
        time.sleep(90)
        self.assertTrue({name}.execute("{tool}", {{}}))


if __name__ == "__main__":
    unittest.main()
'''


def _skill_source(name: str, tool: str) -> str:
    return textwrap.dedent(_SKILL_SOURCE.format(name=name, tool=tool))


def _test_source(template: str, name: str, tool: str) -> str:
    cls = "".join(part.title() for part in name.split("_"))
    return textwrap.dedent(template.format(name=name, tool=tool, cls=cls))


class ForgeTestGateRegistryTest(unittest.TestCase):
    """registry.register_external_skill 的测试门禁行为。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.main_dir = self.root / "app" / "skills"
        self.appspace_dir = self.root / "app-space" / "skills"
        self.main_dir.mkdir(parents=True)
        self.appspace_dir.mkdir(parents=True)
        self.registry = SkillRegistry(
            str(self.main_dir), extra_skills_dirs=[str(self.appspace_dir)]
        )
        self.registry.discover()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _register(
        self,
        name: str,
        tool: str,
        test_code: str | None = None,
        test_timeout: float = 60.0,
    ) -> tuple[bool, str]:
        return self.registry.register_external_skill(
            str(self.appspace_dir),
            f"{name}.py",
            _skill_source(name, tool),
            test_code=test_code,
            test_timeout=test_timeout,
        )

    def test_passing_test_registers_and_marks_tested(self):
        ok, message = self._register(
            "gate_echo", "gate_echo_tool",
            test_code=_test_source(_PASSING_TEST, "gate_echo", "gate_echo_tool"),
        )
        self.assertTrue(ok, message)
        self.assertIn("测试验证通过", message)
        self.assertTrue((self.appspace_dir / "gate_echo.py").is_file())
        # tested 旁车标记存在
        self.assertTrue((self.appspace_dir / "gate_echo.tested").is_file())
        self.assertEqual(self.registry.dispatch("gate_echo_tool", {"text": "hi"}), "echo:hi")

    def test_failing_test_rejects_and_leaves_no_file(self):
        ok, message = self._register(
            "gate_bad", "gate_bad_tool",
            test_code=_test_source(_FAILING_TEST, "gate_bad", "gate_bad_tool"),
        )
        self.assertFalse(ok)
        self.assertIn("测试验证失败", message)
        # 失败摘要包含尾部输出（unittest 的 FAIL 信息）
        self.assertIn("FAIL", message)
        self.assertFalse((self.appspace_dir / "gate_bad.py").exists())
        self.assertFalse((self.appspace_dir / "gate_bad.tested").exists())
        self.assertNotIn("gate_bad", self.registry.skills)

    def test_test_code_syntax_error_rejects(self):
        ok, message = self._register(
            "gate_syn", "gate_syn_tool", test_code="def broken(:\n"
        )
        self.assertFalse(ok)
        self.assertIn("测试代码语法校验失败", message)
        self.assertFalse((self.appspace_dir / "gate_syn.py").exists())

    def test_test_code_over_line_limit_rejects(self):
        padding = "\n".join(f"# 填充行 {i}" for i in range(501))
        ok, message = self._register(
            "gate_big", "gate_big_tool", test_code=f"import unittest\n{padding}\n"
        )
        self.assertFalse(ok)
        self.assertIn("测试代码行数", message)
        self.assertFalse((self.appspace_dir / "gate_big.py").exists())

    def test_test_timeout_rejects(self):
        ok, message = self._register(
            "gate_slow", "gate_slow_tool",
            test_code=_test_source(_SLOW_TEST, "gate_slow", "gate_slow_tool"),
            test_timeout=1.0,  # 缩短超时，避免真实等待 90s
        )
        self.assertFalse(ok)
        self.assertIn("超时", message)
        self.assertFalse((self.appspace_dir / "gate_slow.py").exists())
        self.assertNotIn("gate_slow", self.registry.skills)

    def test_no_test_code_registers_but_marks_untested(self):
        ok, message = self._register("gate_plain", "gate_plain_tool")
        self.assertTrue(ok, message)
        self.assertIn("未附测试", message)
        self.assertFalse((self.appspace_dir / "gate_plain.tested").exists())
        self.assertTrue((self.appspace_dir / "gate_plain.py").is_file())

    def test_blank_test_code_counts_as_untested(self):
        ok, message = self._register(
            "gate_blank", "gate_blank_tool", test_code="   \n  "
        )
        self.assertTrue(ok, message)
        self.assertIn("未附测试", message)
        self.assertFalse((self.appspace_dir / "gate_blank.tested").exists())

    def test_unregister_cleans_tested_marker(self):
        ok, _ = self._register(
            "gate_mark", "gate_mark_tool",
            test_code=_test_source(_PASSING_TEST, "gate_mark", "gate_mark_tool"),
        )
        self.assertTrue(ok)
        marker = self.appspace_dir / "gate_mark.tested"
        self.assertTrue(marker.is_file())
        ok, _ = self.registry.unregister_external_skill("gate_mark")
        self.assertTrue(ok)
        self.assertFalse(marker.exists())


class ForgeTestGateSkillTest(unittest.TestCase):
    """skill_forge 全链路：forge/list/remove 的 tested 标注。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.main_dir = self.root / "app" / "skills"
        self.appspace_dir = self.root / "app-space" / "skills"
        self.main_dir.mkdir(parents=True)
        self.appspace_dir.mkdir(parents=True)
        real_forge = (
            Path(__file__).resolve().parents[1] / "skills" / "skill_forge.py"
        )
        shutil.copy(real_forge, self.main_dir / "skill_forge.py")
        self.registry = SkillRegistry(
            str(self.main_dir), extra_skills_dirs=[str(self.appspace_dir)]
        )
        self.registry.discover()
        forge_module = self.registry.skills["skill_forge"]
        forge_module.configure_runtime(agent=None, registry=self.registry)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _forge(self, name: str, tool: str, test_code: str | None = None) -> dict:
        args: dict = {
            "name": name,
            "description": "测试技能",
            "source_code": _skill_source(name, tool),
        }
        if test_code is not None:
            args["test_code"] = test_code
        return json.loads(self.registry.dispatch("forge_skill", args))

    def _list(self) -> dict:
        return json.loads(self.registry.dispatch("list_forged_skills", {}))

    def test_forge_with_passing_test_marks_tested(self):
        forged = self._forge(
            "fg_echo", "fg_echo_tool",
            test_code=_test_source(_PASSING_TEST, "fg_echo", "fg_echo_tool"),
        )
        self.assertTrue(forged["forged"], forged)
        self.assertTrue(forged["tested"])
        self.assertIn("已注册（含测试验证）", forged["message"])

        listed = self._list()
        self.assertEqual(listed["count"], 1)
        self.assertTrue(listed["skills"][0]["tested"])

        # remove 时标记文件一并清理
        removed = json.loads(
            self.registry.dispatch("remove_forged_skill", {"name": "fg_echo"})
        )
        self.assertTrue(removed["removed"], removed)
        self.assertFalse((self.appspace_dir / "fg_echo.tested").exists())

    def test_forge_without_test_marks_untested(self):
        forged = self._forge("fg_plain", "fg_plain_tool")
        self.assertTrue(forged["forged"], forged)
        self.assertFalse(forged["tested"])
        self.assertIn("已注册（未附测试，建议补充）", forged["message"])

        listed = self._list()
        self.assertEqual(listed["count"], 1)
        self.assertFalse(listed["skills"][0]["tested"])

    def test_forge_with_failing_test_rejected(self):
        forged = self._forge(
            "fg_bad", "fg_bad_tool",
            test_code=_test_source(_FAILING_TEST, "fg_bad", "fg_bad_tool"),
        )
        self.assertFalse(forged["forged"])
        self.assertIn("测试验证失败", forged["message"])
        self.assertFalse((self.appspace_dir / "fg_bad.py").exists())
        self.assertEqual(self._list()["count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
