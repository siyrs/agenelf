from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.evaluate_pip_audit import evaluate  # noqa: E402


class PipAuditPolicyTest(unittest.TestCase):
    def test_clean_report(self):
        result = evaluate({"dependencies": [{"name": "x", "version": "1", "vulns": []}]})
        self.assertEqual(result["finding_count"], 0)
        self.assertEqual(result["fixable_count"], 0)

    def test_unfixed_advisory_is_visible_but_not_fixable(self):
        result = evaluate(
            {
                "dependencies": [
                    {
                        "name": "ecdsa",
                        "version": "0.19.0",
                        "vulns": [
                            {"id": "CVE-EXAMPLE", "aliases": [], "fix_versions": []}
                        ],
                    }
                ]
            }
        )
        self.assertEqual(result["unfixed_count"], 1)
        self.assertEqual(result["fixable_count"], 0)

    def test_fixable_vulnerability_blocks(self):
        result = evaluate(
            {
                "dependencies": [
                    {
                        "name": "demo",
                        "version": "1.0",
                        "vulns": [
                            {
                                "id": "GHSA-demo",
                                "aliases": ["CVE-demo"],
                                "fix_versions": ["1.1"],
                            }
                        ],
                    }
                ]
            }
        )
        self.assertEqual(result["fixable_count"], 1)
        self.assertEqual(result["fixable"][0]["fix_versions"], ["1.1"])

    def test_invalid_report_rejected(self):
        with self.assertRaises(ValueError):
            evaluate([])


if __name__ == "__main__":
    unittest.main()
