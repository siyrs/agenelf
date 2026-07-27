"""growth_pulse 技能的协议与行为测试（离线自主补丁演示产物）。"""

from __future__ import annotations

import unittest

from skills import growth_pulse


class GrowthPulseSkillTest(unittest.TestCase):
    """校验技能协议三件套与 execute 行为，独立可过、不依赖其他技能。"""

    def test_skill_meta(self):
        self.assertEqual(growth_pulse.SKILL_META["name"], "growth_pulse")
        self.assertTrue(growth_pulse.SKILL_META["description"])
        self.assertTrue(growth_pulse.SKILL_META["version"])

    def test_tools_schema(self):
        self.assertIsInstance(growth_pulse.TOOLS, list)
        self.assertEqual(len(growth_pulse.TOOLS), 1)
        function = growth_pulse.TOOLS[0]["function"]
        self.assertEqual(function["name"], "growth_pulse")
        self.assertEqual(function["parameters"]["type"], "object")
        self.assertIn("topic", function["parameters"]["properties"])

    def test_execute_returns_pulse_text(self):
        result = growth_pulse.execute(
            "growth_pulse", {"topic": "离线演示", "skill_count": 3}
        )
        self.assertIsInstance(result, str)
        self.assertTrue(result.strip())
        self.assertIn("成长脉动", result)
        self.assertIn("离线演示", result)
        self.assertIn("3 个技能", result)

    def test_execute_defaults_and_unknown_tool(self):
        self.assertTrue(growth_pulse.execute("growth_pulse", {}).strip())
        self.assertIn("未知工具", growth_pulse.execute("missing", {}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
