from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import authorized_upgrade


class NodeUpgradePolicyTest(unittest.TestCase):
    def test_node_scopes_expand_to_runtime_build_contracts_and_both_test_roots(self) -> None:
        plan = authorized_upgrade.make_plan(
            "升级 Node.js Agent Core、事件 JSON Schema 和 Dockerfile.node",
            scopes=["node_runtime", "contracts", "node_build"],
        )
        self.assertIn("node_runtime", plan["scopes"])
        self.assertIn("node_tests", plan["scopes"])
        self.assertIn("tests", plan["scopes"])
        self.assertIn("node/packages/core/", plan["allowed_paths"])
        self.assertIn("node/tests/", plan["allowed_paths"])
        self.assertIn("app/tests/", plan["allowed_paths"])
        self.assertIn("contracts/", plan["allowed_paths"])
        self.assertIn("Dockerfile.node", plan["allowed_paths"])
        self.assertEqual(
            authorized_upgrade.NODE_UPGRADE_POLICY_VERSION,
            "owner-authorized-node-upgrade-v1",
        )

    def test_node_goal_is_classified_without_replacing_exact_owner_scopes(self) -> None:
        scopes = authorized_upgrade.classify_scopes(
            "完善 TypeScript validation runner 与 node/tests 回归"
        )
        self.assertIn("node_runners", scopes)
        self.assertIn("node_tests", scopes)
        self.assertIn("tests", scopes)

    def test_typescript_paths_and_control_plane_basenames_are_supported(self) -> None:
        allowed = authorized_upgrade.expand_allowed_paths(
            ["node_runtime", "node_build", "compose"]
        )
        self.assertEqual(
            authorized_upgrade.validate_repo_path(
                "node/packages/core/src/new-feature.ts", allowed
            ),
            "node/packages/core/src/new-feature.ts",
        )
        self.assertEqual(
            authorized_upgrade.validate_repo_path("Dockerfile.control-plane", allowed),
            "Dockerfile.control-plane",
        )
        with self.assertRaises(authorized_upgrade.AuthorizedUpgradeError):
            authorized_upgrade.validate_repo_path("local/secrets/key", allowed)

    def test_existing_node_tests_are_immutable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agenelf-node-upgrade-policy-") as temp:
            repo = Path(temp)
            test_path = repo / "node" / "tests" / "existing.test.ts"
            test_path.parent.mkdir(parents=True)
            test_path.write_text("export const value = 1;\n", encoding="utf-8")
            before = hashlib.sha256(test_path.read_bytes()).hexdigest()
            session = {
                "plan": {
                    "allowed_paths": ["node/tests/"],
                    "max_files": 2,
                    "max_changed_lines": 100,
                }
            }
            with self.assertRaisesRegex(
                authorized_upgrade.AuthorizedUpgradeError,
                "既有 Node 测试受保护",
            ):
                authorized_upgrade._prepare_changes(
                    session,
                    repo,
                    {"node/tests/existing.test.ts": before},
                    {"node/tests/existing.test.ts": "export const value = 2;\n"},
                )

    def test_node_production_change_requires_new_regression_test(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agenelf-node-upgrade-policy-") as temp:
            repo = Path(temp)
            target = repo / "node" / "packages" / "core" / "src" / "feature.ts"
            target.parent.mkdir(parents=True)
            target.write_text("export const value = 1;\n", encoding="utf-8")
            session = {
                "plan": {
                    "allowed_paths": ["node/packages/core/", "node/tests/"],
                    "max_files": 3,
                    "max_changed_lines": 200,
                }
            }
            with mock.patch(
                "core.node_upgrade_policy._validate_node_syntax",
                return_value=None,
            ):
                with self.assertRaisesRegex(
                    authorized_upgrade.AuthorizedUpgradeError,
                    "必须新增 app/tests/test_.* 或 node/tests/.*",
                ):
                    authorized_upgrade._prepare_changes(
                        session,
                        repo,
                        {},
                        {
                            "node/packages/core/src/feature.ts":
                                "export const value = 2;\n"
                        },
                    )

                records = authorized_upgrade._prepare_changes(
                    session,
                    repo,
                    {},
                    {
                        "node/packages/core/src/feature.ts":
                            "export const value = 2;\n",
                        "node/tests/feature.test.ts":
                            "import test from 'node:test';\n"
                            "test('feature', () => {});\n",
                    },
                )
            self.assertEqual(
                {item["path"] for item in records},
                {
                    "node/packages/core/src/feature.ts",
                    "node/tests/feature.test.ts",
                },
            )

    def test_node_redlines_block_shell_dynamic_code_tls_bypass_and_lifecycle_scripts(self) -> None:
        rejected = {
            "node/packages/core/src/bad.ts":
                "import { exec } from 'node:child_process';\nexec('id');\n",
            "node/packages/core/src/eval.ts": "eval(ownerInput);\n",
            "node/packages/core/src/tls.ts":
                "process.env.NODE_TLS_REJECT_UNAUTHORIZED = '0';\n",
            "package.json":
                '{"name":"agenelf","scripts":{"postinstall":"node setup.js"}}',
        }
        for path, content in rejected.items():
            with self.subTest(path=path):
                with self.assertRaises(authorized_upgrade.AuthorizedUpgradeError):
                    authorized_upgrade.scan_redlines(path, content)

    def test_safe_node_candidate_does_not_trigger_redlines(self) -> None:
        authorized_upgrade.scan_redlines(
            "node/packages/core/src/safe.ts",
            "export function status(): string { return 'ok'; }\n",
        )
        authorized_upgrade.scan_redlines(
            "package.json",
            '{"name":"agenelf","scripts":{"test:node":"node --test"}}',
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
