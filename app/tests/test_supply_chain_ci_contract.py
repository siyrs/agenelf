"""供应链与 CI 门禁契约测试。

断言 .github/workflows/ 下的 GitHub Actions 配置持续满足治理基线：
security.yml 的供应链门禁齐全、ci.yml 保留测试与治理校验、
所有 workflow 显式声明最小权限、CodeQL 模板存在。
"""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = PROJECT_ROOT / ".github" / "workflows"

def _jobs(document: dict) -> dict:
    jobs = document.get("jobs")
    return jobs if isinstance(jobs, dict) else {}


def _job_has_meaningful_steps(job: dict) -> bool:
    steps = job.get("steps")
    if not isinstance(steps, list):
        return False
    run_steps = [step for step in steps if isinstance(step, dict) and step.get("run")]
    return len(steps) >= 3 or bool(run_steps)


class SupplyChainCiContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflows = {}
        for path in sorted(WORKFLOWS_DIR.glob("*.yml")):
            cls.workflows[path.name] = yaml.safe_load(path.read_text(encoding="utf-8"))

    def test_security_workflow_exists_with_supply_chain_jobs(self):
        self.assertIn("security.yml", self.workflows)
        jobs = _jobs(self.workflows["security.yml"])
        required = {
            "governance",
            "dependency-audit",
            "secret-scan",
            "sbom",
            "shellcheck",
        }
        missing = required - set(jobs)
        self.assertFalse(missing, f"security.yml 缺少门禁 job：{sorted(missing)}")

    def test_security_jobs_have_meaningful_steps(self):
        jobs = _jobs(self.workflows["security.yml"])
        for name in ("governance", "dependency-audit", "secret-scan", "sbom", "shellcheck"):
            with self.subTest(job=name):
                self.assertIn(name, jobs)
                self.assertTrue(
                    _job_has_meaningful_steps(jobs[name]),
                    f"job {name} 需要至少 3 个 step 或包含 run 命令",
                )

    def test_security_workflow_covers_expected_tools(self):
        text = (WORKFLOWS_DIR / "security.yml").read_text(encoding="utf-8")
        for token in ("validate_governance.py", "pip-audit", "gitleaks", "cyclonedx", "shellcheck"):
            self.assertIn(token, text)

    def test_ci_still_runs_unittest_and_governance_validation(self):
        self.assertIn("ci.yml", self.workflows)
        text = (WORKFLOWS_DIR / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("unittest discover", text)
        self.assertIn("validate_governance.py", text)

    def test_all_workflows_declare_explicit_minimal_permissions(self):
        for name, document in self.workflows.items():
            with self.subTest(workflow=name):
                self.assertIsInstance(document, dict)
                permissions = document.get("permissions")
                self.assertIsInstance(
                    permissions,
                    dict,
                    f"{name} 必须显式声明 permissions 映射（最小权限）",
                )
                self.assertTrue(permissions, f"{name} 的 permissions 不能为空")
                forbidden_grants = {"write-all", "read-all"}
                self.assertFalse(
                    forbidden_grants & set(permissions),
                    f"{name} 不应使用宽权限 {sorted(forbidden_grants)}",
                )

    def test_codeql_template_exists(self):
        self.assertIn("codeql.yml", self.workflows)
        document = self.workflows["codeql.yml"]
        text = (WORKFLOWS_DIR / "codeql.yml").read_text(encoding="utf-8")
        self.assertIn("github/codeql-action", text)
        self.assertIn("python", text)
        self.assertTrue(_jobs(document), "codeql.yml 必须定义 analyze job")


if __name__ == "__main__":
    unittest.main(verbosity=2)
