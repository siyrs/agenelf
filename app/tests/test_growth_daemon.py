"""成长守护进程（scripts/growth_daemon.sh）集成测试。

临时目录布局 + `--once` 本地直调模式实测：
- 反思计数每轮 +1（local/self/reflections.json）；
- optimize_auto 被执行（growth.log 中的返回摘要可证）；
- logs/growth.log 落统一 JSON 行；
- docker 不可用/不存在时优雅降级为本地直调。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# 保证从仓库根目录导入 core 包
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "growth_daemon.sh"
APP_DIR = REPO_ROOT / "app"


class GrowthDaemonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "logs").mkdir(parents=True)
        (self.root / "local" / "self").mkdir(parents=True)
        # 守护脚本按 <根>/app 定位代码；临时根用符号链接复用真实 app
        (self.root / "app").symlink_to(APP_DIR, target_is_directory=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _run_once(self, *, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["AGENELF_ROOT"] = str(self.root)
        env["AGENELF_GROWTH_DOCKER"] = "0"  # 强制本地直调，结果确定性
        # 清理可能被其他测试用例遗留的路径覆盖，保证状态落进临时根
        for key in ("AGENELF_SELF_DIR", "AGENELF_LOCAL_DIR"):
            env.pop(key, None)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash", str(SCRIPT), "--once"],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )

    def _reflections(self) -> list[dict]:
        path = self.root / "local" / "self" / "reflections.json"
        if not path.is_file():
            return []
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []

    def _growth_log_lines(self) -> list[dict]:
        path = self.root / "logs" / "growth.log"
        self.assertTrue(path.is_file(), "logs/growth.log 应落盘")
        lines = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if raw:
                lines.append(json.loads(raw))  # 每一行都必须是合法 JSON
        return lines

    def test_once_records_reflection_and_json_log(self):
        before = len(self._reflections())
        completed = self._run_once()
        self.assertEqual(completed.returncode, 0, completed.stderr)

        # 1. 反思计数 +1，trigger 留痕为 growth_daemon
        reflections = self._reflections()
        self.assertEqual(len(reflections), before + 1)
        self.assertEqual(reflections[-1].get("trigger"), "growth_daemon")
        self.assertFalse(reflections[-1].get("consciousness_claim", True))

        # 2. growth.log 落统一 JSON 行，覆盖全部三个动作
        actions = {line.get("action"): line for line in self._growth_log_lines()}
        for expected in ("round_start", "reflect", "optimize_auto", "capability_health", "round_done"):
            self.assertIn(expected, actions)
            self.assertTrue(actions[expected].get("ok"), f"{expected} 应成功")
            self.assertIn("ts", actions[expected])
        # 反思摘要可核查
        reflect_summary = actions["reflect"]["summary"]
        self.assertEqual(reflect_summary.get("reflections_total"), before + 1)
        # 3. optimize_auto 被执行：返回摘要含 note 与 auto_rollbacks 字段
        optimize_summary = actions["optimize_auto"]["summary"]
        self.assertIn("note", optimize_summary)
        self.assertIn("auto_rollbacks", optimize_summary)

    def test_once_is_repeatable_and_increments_each_round(self):
        first = self._run_once()
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self._run_once()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(len(self._reflections()), 2)

    def test_graceful_local_fallback_when_docker_unusable(self):
        # 构造一个必然失败的假 docker：探测应失败并优雅降级本地直调
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        fake_docker = fake_bin / "docker"
        fake_docker.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
        fake_docker.chmod(0o755)
        # 不强制 AGENELF_GROWTH_DOCKER=0：自动探测失败应优雅降级本地直调
        env2 = dict(os.environ)
        env2["AGENELF_ROOT"] = str(self.root)
        env2.pop("AGENELF_GROWTH_DOCKER", None)
        # 清理可能被其他测试用例遗留的路径覆盖，保证状态落进临时根
        for key in ("AGENELF_SELF_DIR", "AGENELF_LOCAL_DIR"):
            env2.pop(key, None)
        env2["PATH"] = f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"
        completed = subprocess.run(
            ["bash", str(SCRIPT), "--once"],
            capture_output=True,
            text=True,
            env=env2,
            timeout=120,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("mode=local", completed.stdout)
        self.assertEqual(len(self._reflections()), 1)

    def test_help_and_unknown_argument(self):
        help_run = subprocess.run(
            ["bash", str(SCRIPT), "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(help_run.returncode, 0)
        self.assertIn("cron", help_run.stdout)
        bad_run = subprocess.run(
            ["bash", str(SCRIPT), "--bogus"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(bad_run.returncode, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
