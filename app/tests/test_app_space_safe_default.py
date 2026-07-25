from __future__ import annotations
import os
import tempfile
import unittest
from pathlib import Path

from core.agent import Agent

SAFE_SKILL = '''
SKILL_META = {"name": "safe_builtin", "description": "safe", "version": "1"}
TOOLS = [{"type": "function", "function": {"name": "safe_tool", "description": "safe", "parameters": {"type": "object", "properties": {}, "required": []}}}]
def execute(tool_name, args): return "safe"
'''
MALICIOUS_SKILL = '''
from pathlib import Path
Path(__file__).with_name("loaded.marker").write_text("loaded", encoding="utf-8")
SKILL_META = {"name": "untrusted", "description": "untrusted", "version": "1"}
TOOLS = [{"type": "function", "function": {"name": "untrusted_tool", "description": "x", "parameters": {"type": "object", "properties": {}, "required": []}}}]
def execute(tool_name, args): return "x"
'''

class AppSpaceSafeDefaultTest(unittest.TestCase):
    def test_app_space_is_not_imported_without_explicit_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skills = root / "skills"
            appspace = root / "app-space" / "skills"
            local = root / "local"
            skills.mkdir(); appspace.mkdir(parents=True); (local / "memory").mkdir(parents=True); (local / "self").mkdir()
            (skills / "safe_builtin.py").write_text(SAFE_SKILL, encoding="utf-8")
            (appspace / "untrusted.py").write_text(MALICIOUS_SKILL, encoding="utf-8")
            old = {key: os.environ.get(key) for key in ("AGENELF_ROOT", "AGENELF_ENABLE_APP_SPACE_SKILLS")}
            os.environ["AGENELF_ROOT"] = str(root)
            os.environ.pop("AGENELF_ENABLE_APP_SPACE_SKILLS", None)
            try:
                agent = Agent({
                    "mock": True,
                    "runtime_root": str(root),
                    "local_dir": str(local),
                    "self_dir": str(local / "self"),
                    "memory_path": str(local / "memory" / "memory.json"),
                    "skills_dir": str(skills),
                    "persona_path": str(root / "missing.yaml"),
                    "agent": {"name": "Test"},
                })
            finally:
                for key, value in old.items():
                    if value is None: os.environ.pop(key, None)
                    else: os.environ[key] = value
            self.assertIn("safe_builtin", agent.registry.skills)
            self.assertNotIn("untrusted", agent.registry.skills)
            self.assertFalse((appspace / "loaded.marker").exists())
            self.assertIn("直接技能热加载已禁用", agent.evolve_skill("anything"))

if __name__ == "__main__":
    unittest.main(verbosity=2)
