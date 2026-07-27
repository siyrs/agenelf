"""同进程多 Agent/Registry 实例的运行时隔离测试。

覆盖两类历史问题：
1. registry 用 ``sys.modules["agenelf_skill_<name>"]`` 全局键加载技能模块，
   多实例或 reload 时互相覆盖/泄漏；现在模块键带唯一后缀且旧键被清理。
2. 技能用模块级全局 ``_AGENT`` 缓存 configure_runtime 绑定的 agent，
   同进程第二个 Agent 实例污染第一个；现在状态优先写入
   Registry 实例的 per-instance ``runtime_context``。
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.agent import Agent
from core.registry import SkillRegistry

REAL_SKILLS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "skills"))

_KEY_PREFIX = "agenelf_skill_"


def _skill_module_keys() -> set[str]:
    return {key for key in sys.modules if key.startswith(_KEY_PREFIX)}


class _FakeAgent:
    """最小化 Agent 替身：提供有状态技能绑定与执行所需的接口。"""

    def __init__(self, name: str, registry: SkillRegistry):
        self.name = name
        self.registry = registry
        self.config: dict = {}

    def local_status(self):
        return {"agent": self.name}

    def reload_local_context(self):
        return {"agent": self.name}

    def remember_owner(self, kind, content):
        return {"agent": self.name, "kind": kind, "content": content}

    def recall_owner(self, query, limit=5):
        return [f"{self.name}:{query}:{limit}"]

    def self_development_status(self):
        return {"agent": self.name}


_STATEFUL_SKILLS = (
    "local_context",
    "self_development",
    "self_optimize",
    "self_reflection",
    "software_validation",
    "code_repair",
    "skill_forge",
    "runtime_doctor",
    "authorized_self_upgrade",
)


class RegistryModuleKeyTest(unittest.TestCase):
    """技能模块键的唯一性与生命周期清理。"""

    def test_module_keys_are_unique_per_instance(self):
        baseline = _skill_module_keys()
        reg1 = SkillRegistry(REAL_SKILLS_DIR)
        reg2 = SkillRegistry(REAL_SKILLS_DIR)
        reg1.discover()
        reg2.discover()
        try:
            self.assertTrue(reg1.skills)
            self.assertEqual(set(reg1.skills), set(reg2.skills))
            keys1 = {module.__name__ for module in reg1.skills.values()}
            keys2 = {module.__name__ for module in reg2.skills.values()}
            # 命名规则：agenelf_skill_<name>_<8 位 hex>
            for name, module in reg1.skills.items():
                self.assertRegex(
                    module.__name__, rf"^{_KEY_PREFIX}{re.escape(name)}_[0-9a-f]{{8}}$"
                )
            # 两个实例的键互不重叠（不会互相覆盖 sys.modules）
            self.assertFalse(keys1 & keys2)
            # 同名技能在两个实例中是不同的模块对象
            for name in reg1.skills:
                self.assertIsNot(reg1.skills[name], reg2.skills[name])
            # sys.modules 净增量 = 两个实例加载的模块总数（无覆盖、无泄漏）
            self.assertEqual(
                len(_skill_module_keys() - baseline), len(keys1) + len(keys2)
            )
            for key in keys1 | keys2:
                self.assertIn(key, sys.modules)
        finally:
            for reg in (reg1, reg2):
                for name in list(reg.skills):
                    reg._untrack_module(name)

    def test_reload_cleans_old_key(self):
        baseline = _skill_module_keys()
        reg = SkillRegistry(REAL_SKILLS_DIR)
        reg.discover()
        try:
            module = reg.skills["local_context"]
            old_key = module.__name__
            self.assertIn(old_key, sys.modules)
            self.assertTrue(reg.reload("local_context"))
            new_key = reg.skills["local_context"].__name__
            self.assertNotEqual(old_key, new_key)
            self.assertNotIn(old_key, sys.modules)
            self.assertIn(new_key, sys.modules)
            # reload 后净增量保持不变（旧键已清理）
            self.assertEqual(len(_skill_module_keys() - baseline), len(reg.skills))
            # reload 不存在的技能：返回 False 且不产生新键
            self.assertFalse(reg.reload("no_such_skill"))
            self.assertEqual(len(_skill_module_keys() - baseline), len(reg.skills))
        finally:
            for name in list(reg.skills):
                reg._untrack_module(name)

    def test_failed_load_leaves_no_sys_modules_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "broken_protocol.py"), "w", encoding="utf-8") as fh:
                fh.write("SKILL_META = {}\n")  # 缺 TOOLS/execute，校验必失败
            with open(os.path.join(tmpdir, "syntax_error.py"), "w", encoding="utf-8") as fh:
                fh.write("def broken(:\n")  # exec_module 必失败
            baseline = _skill_module_keys()
            reg = SkillRegistry(tmpdir)
            reg.discover()
            self.assertIn("broken_protocol", reg.errors)
            self.assertIn("syntax_error", reg.errors)
            self.assertFalse(reg.skills)
            # 加载失败的模块不得泄漏 sys.modules 键
            self.assertEqual(_skill_module_keys() - baseline, set())

    def test_bind_and_get_state_roundtrip(self):
        reg = SkillRegistry(REAL_SKILLS_DIR)
        marker = object()
        reg.bind_state("probe", agent=marker, config={"a": 1})
        reg.bind_state("probe", extra="merged")
        state = reg.get_state("probe")
        self.assertIs(state["agent"], marker)
        self.assertEqual(state["config"], {"a": 1})
        self.assertEqual(state["extra"], "merged")
        self.assertEqual(reg.get_state("missing"), {})


class RuntimeStateIsolationTest(unittest.TestCase):
    """两个 Registry 实例各自绑定有状态技能，互不污染。"""

    def setUp(self):
        self.reg1 = SkillRegistry(REAL_SKILLS_DIR)
        self.reg2 = SkillRegistry(REAL_SKILLS_DIR)
        self.reg1.discover()
        self.reg2.discover()
        self.agent1 = _FakeAgent("agent-1", self.reg1)
        self.agent2 = _FakeAgent("agent-2", self.reg2)
        # 模拟 Agent.configure_skill_runtimes 的绑定调用
        for reg, agent in ((self.reg1, self.agent1), (self.reg2, self.agent2)):
            for name in _STATEFUL_SKILLS:
                module = reg.skills.get(name)
                if module is None:
                    continue
                module.configure_runtime(agent=agent, registry=reg, config=agent.config)

    def tearDown(self):
        for reg in (self.reg1, self.reg2):
            for name in list(reg.skills):
                reg._untrack_module(name)

    def test_bind_state_is_per_instance(self):
        for name in _STATEFUL_SKILLS:
            if name not in self.reg1.skills:
                continue
            self.assertIs(self.reg1.get_state(name)["agent"], self.agent1, name)
            self.assertIs(self.reg2.get_state(name)["agent"], self.agent2, name)

    def test_module_globals_not_shared_across_instances(self):
        mod1 = self.reg1.skills["local_context"]
        mod2 = self.reg2.skills["local_context"]
        # 唯一模块键 => 各自持有独立的模块全局，不再互相覆盖
        self.assertIs(mod1._agent, self.agent1)
        self.assertIs(mod2._agent, self.agent2)
        self.assertIs(mod1._registry, self.reg1)
        self.assertIs(mod2._registry, self.reg2)
        mod1 = self.reg1.skills["self_development"]
        mod2 = self.reg2.skills["self_development"]
        self.assertIs(mod1._AGENT, self.agent1)
        self.assertIs(mod2._AGENT, self.agent2)

    def test_execution_reads_own_agent(self):
        mod1 = self.reg1.skills["local_context"]
        mod2 = self.reg2.skills["local_context"]
        result1 = json.loads(mod1.execute("get_local_context_status", {}))
        result2 = json.loads(mod2.execute("get_local_context_status", {}))
        self.assertEqual(result1["agent"], "agent-1")
        self.assertEqual(result2["agent"], "agent-2")
        dev1 = json.loads(self.reg1.skills["self_development"].execute("self_development_status", {}))
        dev2 = json.loads(self.reg2.skills["self_development"].execute("self_development_status", {}))
        self.assertEqual(dev1["agent"], "agent-1")
        self.assertEqual(dev2["agent"], "agent-2")
        # 绑定顺序不影响结果：再次执行仍读到各自的 agent
        again = json.loads(mod1.execute("get_local_context_status", {}))
        self.assertEqual(again["agent"], "agent-1")

    def test_reload_keeps_single_key_per_skill(self):
        prefix = f"{_KEY_PREFIX}local_context_"
        before = [key for key in sys.modules if key.startswith(prefix)]
        old_key = self.reg1.skills["local_context"].__name__
        self.assertTrue(self.reg1.reload("local_context"))
        after = [key for key in sys.modules if key.startswith(prefix)]
        # 旧键清理、新键登记，总量不变（不受其他测试遗留键影响）
        self.assertEqual(len(after), len(before))
        self.assertNotIn(old_key, after)
        self.assertIn(self.reg1.skills["local_context"].__name__, after)
        self.assertIn(self.reg2.skills["local_context"].__name__, after)


class TwoAgentProcessIsolationTest(unittest.TestCase):
    """同进程两个真实 Agent 实例共享技能目录，端到端验证互不污染。"""

    _PROBE_SOURCE = '''\
"""测试用有状态技能：按新的 per-instance 绑定模式缓存 agent。"""

SKILL_META = {"name": "echo_agent", "description": "回显绑定的 agent", "version": "0.1.0"}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "whoami",
            "description": "返回当前绑定 agent 的名字",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }
]

_AGENT = None
_REGISTRY = None


def configure_runtime(*, agent=None, registry=None, **_):
    global _AGENT, _REGISTRY
    _AGENT = agent
    if registry is not None and hasattr(registry, "bind_state"):
        _REGISTRY = registry
        registry.bind_state("echo_agent", agent=agent)


def execute(tool_name, args):
    if tool_name != "whoami":
        return f"未知工具：{tool_name}"
    agent = None
    if _REGISTRY is not None and hasattr(_REGISTRY, "get_state"):
        agent = _REGISTRY.get_state("echo_agent").get("agent")
    if agent is None:
        agent = _AGENT
    if agent is None:
        return "agent:<unbound>"
    return "agent:" + agent.config.get("agent", {}).get("name", "?")
'''

    def _build_agent(self, tmpdir: str, skills_dir: str, name: str) -> Agent:
        return Agent(
            {
                "mock": True,
                "skills_dir": skills_dir,
                "memory_path": os.path.join(tmpdir, "memory.json"),
                "persona_path": os.path.join(tmpdir, "persona.yaml"),
                "agent": {"name": name, "max_tool_rounds": 4},
            }
        )

    def test_two_agents_do_not_pollute_each_other(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_dir = os.path.join(tmpdir, "skills")
            os.makedirs(skills_dir, exist_ok=True)
            with open(os.path.join(skills_dir, "echo_agent.py"), "w", encoding="utf-8") as fh:
                fh.write(self._PROBE_SOURCE)
            old_cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                agent1 = self._build_agent(os.path.join(tmpdir, "a1"), skills_dir, "AgentOne")
                agent2 = self._build_agent(os.path.join(tmpdir, "a2"), skills_dir, "AgentTwo")
                os.makedirs(os.path.join(tmpdir, "a1"), exist_ok=True)
                os.makedirs(os.path.join(tmpdir, "a2"), exist_ok=True)

                reg1, reg2 = agent1.registry, agent2.registry
                # 各自加载到独立模块对象与独立 sys.modules 键
                self.assertIsNot(reg1.skills["echo_agent"], reg2.skills["echo_agent"])
                self.assertNotEqual(
                    reg1.skills["echo_agent"].__name__, reg2.skills["echo_agent"].__name__
                )
                # per-instance 状态各自持有自己的 agent
                self.assertIs(reg1.get_state("echo_agent")["agent"], agent1)
                self.assertIs(reg2.get_state("echo_agent")["agent"], agent2)
                # 各自技能执行读到自己的 agent（后创建的 agent2 不污染 agent1）
                self.assertEqual(reg1.skills["echo_agent"].execute("whoami", {}), "agent:AgentOne")
                self.assertEqual(reg2.skills["echo_agent"].execute("whoami", {}), "agent:AgentTwo")
                self.assertEqual(reg1.skills["echo_agent"].execute("whoami", {}), "agent:AgentOne")
                # reload 后旧键清理，重新绑定仍读到自己的 agent
                old_key = reg1.skills["echo_agent"].__name__
                self.assertTrue(reg1.reload("echo_agent"))
                self.assertNotIn(old_key, sys.modules)
                agent1.configure_skill_runtimes("echo_agent")
                self.assertEqual(reg1.skills["echo_agent"].execute("whoami", {}), "agent:AgentOne")
                self.assertEqual(reg2.skills["echo_agent"].execute("whoami", {}), "agent:AgentTwo")
            finally:
                os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main(verbosity=2)
