"""Agent + MockLLM 的端到端 mock 测试。

测试独立于 skills/ 目录中真实技能的实现进度：
在临时目录中创建一个符合技能协议的 dummy 技能，
验证 chat() 能走完至少一轮完整的 tool-call 回路。

同时兼容 pytest 与直接 `python tests/test_agent_mock.py` 运行。
"""

import os
import sys
import tempfile
import unittest

# 保证从仓库根目录导入 core 包
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.agent import Agent
from core.llm import MockLLM

# 符合技能协议的 dummy 技能源码：提供 write_code_file 工具
DUMMY_SKILL_SOURCE = '''\
"""测试用 dummy 技能：模拟写入代码文件。"""

SKILL_META = {"name": "dummy_writer", "description": "模拟写代码文件", "version": "0.1.0"}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "write_code_file",
            "description": "把代码内容写入指定文件",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "content": {"type": "string", "description": "代码内容"},
                },
                "required": ["path", "content"],
            },
        },
    }
]


def execute(tool_name: str, args: dict) -> str:
    """内部捕获所有异常，始终返回字符串。"""
    try:
        if tool_name != "write_code_file":
            return f"未知工具: {tool_name}"
        filename = args.get("path") or args.get("filename", "")
        content = args.get("content", "")
        with open(filename, "w", encoding="utf-8") as f:
            f.write(content)
        return f"已写入文件 {filename}（{len(content)} 字符）"
    except Exception as e:
        return f"执行出错: {e}"
'''


def _build_agent(tmpdir: str) -> Agent:
    """在临时目录中组装一个使用 MockLLM 的 Agent。"""
    skills_dir = os.path.join(tmpdir, "skills")
    os.makedirs(skills_dir, exist_ok=True)
    with open(os.path.join(skills_dir, "dummy_writer.py"), "w", encoding="utf-8") as f:
        f.write(DUMMY_SKILL_SOURCE)

    config = {
        "mock": True,  # 强制 MockLLM
        "skills_dir": skills_dir,
        "memory_path": os.path.join(tmpdir, "memory.json"),
        "persona_path": os.path.join(tmpdir, "persona.yaml"),  # 不存在时回退为空画像
        "agent": {"name": "Agenelf", "max_tool_rounds": 8},
    }
    return Agent(config)


class TestAgentMock(unittest.TestCase):
    """MockLLM 驱动的 Agent tool-call 回路测试。"""

    def test_chat_completes_tool_call_loop(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # dummy 技能写文件的目标位置切到临时目录，避免污染仓库
            old_cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                agent = _build_agent(tmpdir)
                # 确认使用的是 MockLLM 且 dummy 技能已加载
                self.assertIsInstance(agent.llm, MockLLM)
                self.assertIn("dummy_writer", agent.registry.skills)

                # 在 dispatch 外包一层计数，验证至少发生一次工具调用
                dispatch_count = {"n": 0, "results": []}
                original_dispatch = agent.registry.dispatch

                def counting_dispatch(tool_name, args):
                    dispatch_count["n"] += 1
                    result = original_dispatch(tool_name, args)
                    dispatch_count["results"].append(result)
                    return result

                agent.registry.dispatch = counting_dispatch

                reply = agent.chat("帮我写一个 hello world 并运行")

                # 断言 1：最终返回非空字符串
                self.assertIsInstance(reply, str)
                self.assertTrue(reply.strip(), "最终回复不能为空")

                # 断言 2：过程中至少发生一次工具调用
                self.assertGreaterEqual(
                    dispatch_count["n"], 1, "应至少发生一次工具调用"
                )
                self.assertIn("已写入文件", dispatch_count["results"][0])

                # 断言 3：重要交互已写入长期记忆（episode）
                episodes = agent.memory.recall("hello world")
                self.assertTrue(episodes, "交互应写入 episode 记忆")
            finally:
                os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main(verbosity=2)
