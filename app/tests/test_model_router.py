from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from core.model_router import ModelRouter, ModelRouterError
from skills import model_routing


CONFIG = """
providers:
  deepseek:
    enabled: true
    protocol: openai_compatible
    model: deepseek-v4-pro
    api_key_env: DEEPSEEK_API_KEY
    capabilities: [chat, tools, reasoning, coding]
    cost_class: low
    privacy: external
  gpt:
    enabled: true
    protocol: openai_compatible
    model: gpt-coding
    api_key_env: GPT_API_KEY
    capabilities: [chat, tools, reasoning, coding, vision]
    cost_class: high
    privacy: external
  glm:
    enabled: true
    protocol: openai_compatible
    model: glm-code
    api_key_env: GLM_API_KEY
    capabilities: [chat, tools, coding]
    cost_class: medium
    privacy: external
  ollama:
    enabled: true
    protocol: ollama
    model: qwen3-coder
    capabilities: [chat, coding, privacy]
    cost_class: local
    privacy: local
routes:
  routine: [deepseek, glm, ollama]
  reasoning: [gpt, deepseek]
  coding: [deepseek, glm, gpt, ollama]
  privacy: [ollama]
  vision: [gpt]
  voice: [deepseek, glm]
"""


class ModelRouterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "models.yaml"
        self.path.write_text(CONFIG, encoding="utf-8")
        self.old_env = {
            key: os.environ.get(key)
            for key in (
                "DEEPSEEK_API_KEY",
                "GPT_API_KEY",
                "GLM_API_KEY",
                "AGENELF_MODELS_FILE",
            )
        }
        os.environ["DEEPSEEK_API_KEY"] = "secret-deepseek"
        os.environ["GPT_API_KEY"] = "secret-gpt"
        os.environ.pop("GLM_API_KEY", None)

    def tearDown(self):
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_catalog_never_exposes_credentials_or_env_names(self):
        catalog = ModelRouter(self.path).catalog()
        text = json.dumps(catalog, ensure_ascii=False)
        self.assertNotIn("secret-deepseek", text)
        self.assertNotIn("DEEPSEEK_API_KEY", text)
        deepseek = next(item for item in catalog["providers"] if item["alias"] == "deepseek")
        self.assertTrue(deepseek["ready"])
        glm = next(item for item in catalog["providers"] if item["alias"] == "glm")
        self.assertFalse(glm["ready"])

    def test_route_prefers_ready_low_cost_and_has_fallbacks(self):
        result = ModelRouter(self.path).route(
            "coding", required_capabilities=["coding", "tools"], max_cost_class="high"
        )
        self.assertEqual(result["selected"]["alias"], "deepseek")
        self.assertEqual(
            [item["alias"] for item in result["fallback_chain"]],
            ["deepseek", "glm", "gpt"],
        )
        self.assertFalse(result["credentials_exposed"])

    def test_privacy_route_selects_local_model(self):
        result = ModelRouter(self.path).route(
            "privacy", required_capabilities=["privacy"], prefer_local=True
        )
        self.assertEqual(result["selected"]["alias"], "ollama")
        self.assertEqual(result["selected"]["privacy"], "local")

    def test_inline_secret_is_rejected(self):
        bad = self.root / "bad.yaml"
        bad.write_text(
            "providers:\n  bad:\n    protocol: openai_compatible\n    model: x\n    api_key: plaintext\nroutes: {}\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ModelRouterError, "不得.*凭据"):
            ModelRouter(bad)

    def test_skill_uses_owner_local_file(self):
        os.environ["AGENELF_MODELS_FILE"] = str(self.path)
        value = json.loads(
            model_routing.execute(
                "route_model_task",
                {
                    "task_type": "reasoning",
                    "required_capabilities": ["reasoning"],
                    "max_cost_class": "high",
                },
            )
        )
        self.assertTrue(value["ok"], value)
        self.assertEqual(value["selected"]["alias"], "gpt")
        self.assertNotIn("secret-gpt", json.dumps(value))


if __name__ == "__main__":
    unittest.main(verbosity=2)
