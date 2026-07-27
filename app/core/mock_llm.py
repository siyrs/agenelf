"""脚本化 MockLLM：无 API Key 时的本地开发与测试用假 LLM。

与真实 LLMClient（core/llm.py）分离存放，避免 core 主路径携带
离线演示专用的剧本逻辑。自主补丁演示响应不再内嵌技能源码字符串，
而是运行时动态 import skills.growth_pulse 并读取其真实源码，
保证演示产物与仓库内技能实现始终一致、不重复维护。
"""

from __future__ import annotations

from .llm import LLMClient

# 触发 MockLLM 生成工具调用的中文关键词
_TRIGGER_KEYWORDS = ("写", "代码", "运行", "执行", "文件")

# 自主循环补丁请求的特征文本（对应 core/autonomy.py 的补丁提示词；
# 这里硬编码匹配，避免 import autonomy 造成循环依赖）
_AUTONOMY_PROMPT_MARKERS = ("受控自主改进执行器", "【硬性输出契约】")


def _build_autonomy_patch_response() -> str:
    """运行时动态组装离线自主补丁演示响应。

    在函数内 import skills.growth_pulse（而非模块顶层），避免 core 与
    skills 之间的循环依赖；技能源码通过 inspect.getsource 实时读取，
    测试源码从 app/tests/test_growth_pulse.py 读取，两者均与仓库内
    真实文件保持一致，不再维护内嵌副本。
    """
    import inspect
    from pathlib import Path

    from skills import growth_pulse

    skill_source = inspect.getsource(growth_pulse)
    app_root = Path(growth_pulse.__file__).resolve().parent.parent
    test_source = (app_root / "tests" / "test_growth_pulse.py").read_text(
        encoding="utf-8"
    )
    # 脚本化自主补丁响应：仅含两个合规代码块，无多余解释
    return (
        "```python\n# FILE: skills/growth_pulse.py\n" + skill_source + "```\n"
        "\n"
        "```python\n# FILE: tests/test_growth_pulse.py\n" + test_source + "```\n"
    )


class MockLLM(LLMClient):
    """脚本化的假 LLM，用于无 API Key 的本地开发与测试。

    行为脚本：
    0. 任一消息内容包含自主循环特征文本（"受控自主改进执行器" 或
       "【硬性输出契约】"）时，返回一个脚本化但完全合规的自主补丁：
       content 内含 skills/growth_pulse.py 与 tests/test_growth_pulse.py
       两个 ```python 代码块（首行 # FILE: 标记），tool_calls 为空，
       使离线环境也能端到端演示受控自主迭代；
    1. 首轮用户输入中若含 "写"/"代码"/"运行" 等关键词，
       返回一个 write_code_file 或 run_python 的 tool_call；
    2. 若对话中已包含工具结果（role == "tool"），返回最终文本回复；
    3. 其他情况返回普通的提示文本。
    """

    def __init__(self, config: dict | None = None):
        # 不调用父类 __init__，避免任何网络客户端初始化
        self.config = config or {}
        self.model = "mock-llm"
        # 记录 chat 调用次数，便于调试与测试
        self.call_count = 0

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        self.call_count += 1

        # 自主循环补丁请求：命中特征文本时返回脚本化的合规补丁，
        # 保证无 API Key 的离线环境也能演示完整的自主迭代流程
        if any(
            marker in str(m.get("content", ""))
            for m in messages
            for marker in _AUTONOMY_PROMPT_MARKERS
        ):
            return {"content": _build_autonomy_patch_response(), "tool_calls": []}

        # 对话中已存在工具结果 → 生成最终总结回复；
        # 若刚写完示例文件且 run_python 可用，则再补一步"运行它"，演示多轮工具调用
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        if tool_msgs:
            last_result = str(tool_msgs[-1].get("content", ""))
            available = set()
            for t in tools or []:
                fn = t.get("function", {}) if isinstance(t, dict) else {}
                if fn.get("name"):
                    available.add(fn["name"])
            already_ran = any("退出码" in str(m.get("content", "")) for m in tool_msgs)
            if (
                not already_ran
                and "run_python" in available
                and "hello.py" in last_result
                and "写入失败" not in last_result
            ):
                return {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "mock_call_run",
                            "name": "run_python",
                            "arguments": {"code": "exec(open('hello.py', encoding='utf-8').read())"},
                        }
                    ],
                }
            return {
                "content": f"任务已完成，工具执行结果如下：\n{last_result}",
                "tool_calls": [],
            }

        # 取最后一条用户输入判断是否要触发工具调用
        user_text = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_text = str(m.get("content", ""))
                break

        available = set()
        for t in tools or []:
            # OpenAI schema: {"type": "function", "function": {"name": ...}}
            fn = t.get("function", {}) if isinstance(t, dict) else {}
            if fn.get("name"):
                available.add(fn["name"])

        if any(kw in user_text for kw in _TRIGGER_KEYWORDS):
            # 优先调用 write_code_file，其次 run_python；均不可用时伪造一个，
            # 由 registry.dispatch 报未知工具，也能走完一轮完整 tool-call 回路
            if "run_python" in available and "write_code_file" not in available:
                return {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "mock_call_1",
                            "name": "run_python",
                            "arguments": {"code": "print('hello world')"},
                        }
                    ],
                }
            return {
                "content": None,
                "tool_calls": [
                    {
                        "id": "mock_call_1",
                        "name": "write_code_file",
                        "arguments": {
                            "path": "hello.py",
                            "content": "print('hello world')\n",
                        },
                    }
                ],
            }

        return {
            "content": "（MockLLM）未识别到可触发工具的关键词，请描述需要写代码或运行的任务。",
            "tool_calls": [],
        }
