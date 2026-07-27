"""Built-in skill protocol and representative behavior tests."""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SKILL_MODULES = ["code_writer", "ai_tools", "server_ops", "task_handler"]


def _load_skills() -> dict:
    return {name: importlib.import_module(f"skills.{name}") for name in SKILL_MODULES}


class TestSkillProtocol(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skills = _load_skills()

    def test_all_skills_importable(self):
        self.assertEqual(set(self.skills), set(SKILL_MODULES))

    def test_skill_meta_shape(self):
        for name, module in self.skills.items():
            with self.subTest(skill=name):
                meta = getattr(module, "SKILL_META", None)
                self.assertIsInstance(meta, dict)
                self.assertEqual(meta.get("name"), name)
                self.assertTrue(meta.get("description"))
                self.assertIsInstance(meta.get("version"), str)

    def test_tools_schema_and_routing(self):
        for name, module in self.skills.items():
            with self.subTest(skill=name):
                self.assertIsInstance(module.TOOLS, list)
                self.assertTrue(module.TOOLS)
                for tool in module.TOOLS:
                    self.assertEqual(tool.get("type"), "function")
                    function = tool.get("function") or {}
                    self.assertIsInstance(function.get("name"), str)
                    self.assertIsInstance(function.get("description"), str)
                    parameters = function.get("parameters") or {}
                    self.assertEqual(parameters.get("type"), "object")
                    self.assertIn("properties", parameters)
                    self.assertIn("required", parameters)
                    result = module.execute(function["name"], {})
                    self.assertIsInstance(result, str)
                    self.assertFalse(result.startswith("未知工具"))

    def test_unknown_tool_returns_error(self):
        for name, module in self.skills.items():
            with self.subTest(skill=name):
                self.assertIn("未知工具", module.execute("no_such_tool", {}))

    def test_server_ops_declares_composable_capability(self):
        meta = self.skills["server_ops"].CAPABILITY_META
        self.assertEqual(meta["id"], "server.operations")
        risks = {item["name"]: item["risk"] for item in meta["operations"]}
        self.assertEqual(risks["inspect"], "read")
        self.assertEqual(risks["compose_deploy"], "change")
        self.assertIn("software.validation", meta["composes_with"])


class TestCodeWriter(unittest.TestCase):
    def setUp(self):
        self.skill = _load_skills()["code_writer"]
        self.tmp = tempfile.TemporaryDirectory()
        self.skill.set_project_root(self.tmp.name)

    def tearDown(self):
        self.skill.set_project_root(None)
        self.tmp.cleanup()

    def test_write_scratch_and_python_execution_is_disabled(self):
        path_string = self.skill.execute(
            "write_code_file",
            {"path": "notes/hello.py", "content": "print('你好，Agenelf')"},
        )
        written = Path(path_string)
        self.assertTrue(written.is_file())
        self.assertEqual(written.parent.parent, Path(self.tmp.name).resolve())
        result = self.skill.execute("run_python", {"code": "print('never')"})
        self.assertIn("永久禁用", result)

    def test_reject_path_escape(self):
        result = self.skill.execute(
            "write_code_file", {"path": "../evil.py", "content": "x = 1"}
        )
        self.assertIn("逃逸", result)


class TestAiTools(unittest.TestCase):
    def setUp(self):
        self.skill = _load_skills()["ai_tools"]

    def tearDown(self):
        self.skill.set_llm(None)

    def test_mock_and_injected_llm(self):
        self.skill.set_llm(None)
        self.assertIn("mock", self.skill.execute("ask_llm", {"prompt": "hi"}))
        seen = []

        def fake_llm(messages):
            seen.append(messages)
            return "模型回答"

        self.skill.set_llm(fake_llm)
        result = self.skill.execute(
            "ask_llm", {"prompt": "问题", "system": "你是助手"}
        )
        self.assertEqual(result, "模型回答")
        self.assertEqual(seen[0][-1]["content"], "问题")


class TestServerOps(unittest.TestCase):
    def setUp(self):
        self.skill = _load_skills()["server_ops"]
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_root = os.environ.get("AGENELF_ROOT")
        self.old_servers = os.environ.get("AGENELF_SERVERS_FILE")
        os.environ["AGENELF_ROOT"] = str(self.root)
        config = self.root / "servers.yaml"
        config.write_text(
            """servers:
  primary:
    host: 127.0.0.1
    username: agenelf
    managed_root: /srv/agenelf
    allowed_operations: [inspect, docker_ps, service_status, apt_update, compose_deploy, service_restart, docker_install]
    allowed_services: [nginx]
    allowed_bind_roots: [/srv/data]
""",
            encoding="utf-8",
        )
        os.environ["AGENELF_SERVERS_FILE"] = str(config)

    def tearDown(self):
        if self.old_root is None:
            os.environ.pop("AGENELF_ROOT", None)
        else:
            os.environ["AGENELF_ROOT"] = self.old_root
        if self.old_servers is None:
            os.environ.pop("AGENELF_SERVERS_FILE", None)
        else:
            os.environ["AGENELF_SERVERS_FILE"] = self.old_servers
        self.tmp.cleanup()

    def test_server_catalog_hides_auth(self):
        result = self.skill.execute("list_managed_servers", {})
        data = json.loads(result)
        self.assertEqual(data[0]["name"], "primary")
        self.assertNotIn("auth", data[0])

    def test_read_operation_queues_for_runner(self):
        result = self.skill.execute(
            "inspect_server", {"target": "primary", "wait_seconds": 0}
        )
        self.assertIn('"status": "queued"', result)
        self.assertEqual(
            len(list((self.root / "data" / "ops-requests").glob("op-*.json"))), 1
        )

    def test_generic_shell_is_not_exposed_or_confirmable(self):
        tool_names = {tool["function"]["name"] for tool in self.skill.TOOLS}
        self.assertNotIn("run_shell", tool_names)
        target = self.root / "created"
        result = self.skill.run_shell(f"mkdir {target}", confirm=True)
        self.assertIn("已拒绝", result)
        self.assertFalse(target.exists())

    def test_compose_plan_and_red_line(self):
        safe = "services:\n  web:\n    image: nginx:alpine\n"
        result = self.skill.execute(
            "deploy_compose_project",
            {"target": "primary", "project": "demo", "compose_yaml": safe, "plan_only": True},
        )
        self.assertIn("计划校验通过", result)
        unsafe = "services:\n  web:\n    image: nginx\n    privileged: true\n"
        result = self.skill.execute(
            "deploy_compose_project",
            {"target": "primary", "project": "demo", "compose_yaml": unsafe},
        )
        self.assertIn("安全校验失败", result)


class TestTaskHandler(unittest.TestCase):
    def setUp(self):
        self.skill = _load_skills()["task_handler"]
        self.tmp = tempfile.TemporaryDirectory()
        self.skill.set_store_dir(self.tmp.name)

    def tearDown(self):
        self.skill.set_store_dir(None)
        self.tmp.cleanup()

    def test_note_and_todo_persistence(self):
        self.assertIn(
            "已保存",
            self.skill.execute("save_note", {"title": "部署备忘", "content": "先备份"}),
        )
        self.assertEqual(
            self.skill.execute("read_note", {"title": "部署备忘"}), "先备份"
        )
        self.assertIn(
            "已创建 2 条待办",
            self.skill.execute("create_todo", {"items": ["写技能", "跑测试"]}),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
