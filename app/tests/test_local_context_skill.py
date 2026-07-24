from __future__ import annotations

import json
import unittest

from skills import local_context


class FakeAgent:
    def local_status(self):
        return {"profile_loaded": True, "secrets_visible_to_agent": False}

    def reload_local_context(self):
        return {"reloaded": True}

    def remember_owner(self, kind, content):
        return {"stored": True, "kind": kind, "content": content}

    def recall_owner(self, query, limit=5):
        return [f"{query}:{limit}"]


class LocalContextSkillTest(unittest.TestCase):
    def setUp(self):
        local_context.configure_runtime(agent=FakeAgent())

    def test_status_never_exposes_secrets(self):
        result = json.loads(local_context.execute("get_local_context_status", {}))
        self.assertTrue(result["profile_loaded"])
        self.assertFalse(result["secrets_visible_to_agent"])

    def test_remember_and_recall(self):
        saved = json.loads(
            local_context.execute(
                "remember_owner_context", {"kind": "preference", "content": "喜欢树莓派"}
            )
        )
        self.assertTrue(saved["stored"])
        recalled = json.loads(
            local_context.execute("recall_owner_context", {"query": "树莓派", "limit": 3})
        )
        self.assertEqual(recalled["results"], ["树莓派:3"])

    def test_invalid_empty_arguments_are_routed(self):
        self.assertIn("保存失败", local_context.execute("remember_owner_context", {}))
        self.assertIn("检索失败", local_context.execute("recall_owner_context", {}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
