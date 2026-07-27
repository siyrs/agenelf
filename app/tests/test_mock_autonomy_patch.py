"""MockLLM 离线自主补丁生成能力的回归测试。

验证 MockLLM 对自主循环补丁请求返回的脚本化补丁：
1. 可被 core.autonomy._parse_file_blocks 解析；
2. 可通过 core.autonomy.AutonomyEngine._validate_change_set 校验；
3. 写入临时目录副本后，growth_pulse 技能可 import 且其测试文件通过。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from core.autonomy import AutonomyEngine, _parse_file_blocks
from core.mock_llm import MockLLM


def _patch_request_messages() -> list[dict]:
    """构造一条模拟自主引擎发出的补丁请求消息列表。"""
    return [
        {
            "role": "system",
            "content": "你是严谨的 Python 工程师，严格遵守安全补丁契约。",
        },
        {
            "role": "user",
            "content": (
                "你是 Agenelf 的受控自主改进执行器。\n"
                "【目标】\n演示一次离线自主迭代。\n"
                "【硬性输出契约】\n"
                "1. 仅输出需要修改的完整 Python 文件，每个文件使用 ```python 代码块。\n"
                "2. 代码块第一行必须是 # FILE: <相对 app 根目录路径>。\n"
            ),
        },
    ]


class MockAutonomyPatchTest(unittest.TestCase):
    def test_chat_returns_parseable_and_valid_patch(self):
        response = MockLLM().chat(_patch_request_messages(), tools=None)
        self.assertEqual(response["tool_calls"], [])
        content = response["content"]
        self.assertIsInstance(content, str)
        self.assertTrue(content.strip())

        # 补丁可被自主引擎的解析器解析，且恰好包含技能与测试两个文件
        changes = _parse_file_blocks(content)
        self.assertEqual(
            set(changes), {"skills/growth_pulse.py", "tests/test_growth_pulse.py"}
        )

        # 通过自主引擎的变更集静态校验（文件数、必含 tests/test_*.py）
        AutonomyEngine._validate_change_set(changes)

    def test_patch_files_run_in_temp_copy(self):
        response = MockLLM().chat(_patch_request_messages(), tools=None)
        changes = _parse_file_blocks(str(response["content"]))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel_path, body in changes.items():
                target = root / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(body, encoding="utf-8")
            # 补齐包结构，保证临时副本中的 import 行为与真实项目一致
            (root / "skills" / "__init__.py").write_text("", encoding="utf-8")
            (root / "tests" / "__init__.py").write_text("", encoding="utf-8")

            # 在临时副本里能 import growth_pulse 技能并读取协议三件套
            env = dict(os.environ)
            env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
            import_check = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from skills import growth_pulse;"
                    "assert growth_pulse.SKILL_META['name'] == 'growth_pulse';"
                    "assert growth_pulse.TOOLS and callable(growth_pulse.execute)",
                ],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(import_check.returncode, 0, import_check.stderr)

            # subprocess 运行补丁自带的测试文件，退出码必须为 0
            test_run = subprocess.run(
                [sys.executable, str(root / "tests" / "test_growth_pulse.py")],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                test_run.returncode,
                0,
                f"stdout:\n{test_run.stdout}\nstderr:\n{test_run.stderr}",
            )

    def test_normal_messages_not_hijacked(self):
        # 普通对话不含特征文本，不应触发自主补丁分支
        response = MockLLM().chat(
            [{"role": "user", "content": "你好，今天天气如何？"}], tools=None
        )
        self.assertNotIn("# FILE:", str(response["content"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
