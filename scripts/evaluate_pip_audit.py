#!/usr/bin/env python3
"""Evaluate pip-audit JSON with an explicit, reviewable blocking policy.

Unfixed upstream advisories remain visible in the uploaded report but do not make an
upgrade impossible. Any vulnerability with at least one published fix version blocks
CI until dependencies are upgraded or a time-bounded policy exception is reviewed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def evaluate(report: Any) -> dict[str, Any]:
    if not isinstance(report, dict) or not isinstance(report.get("dependencies"), list):
        raise ValueError("pip-audit JSON 必须包含 dependencies 数组")
    findings: list[dict[str, Any]] = []
    fixable: list[dict[str, Any]] = []
    for dependency in report["dependencies"]:
        if not isinstance(dependency, dict):
            continue
        name = str(dependency.get("name", "unknown"))
        version = str(dependency.get("version", "unknown"))
        vulns = dependency.get("vulns", [])
        if not isinstance(vulns, list):
            continue
        for vuln in vulns:
            if not isinstance(vuln, dict):
                continue
            fixes = [str(item) for item in vuln.get("fix_versions", []) if str(item)]
            item = {
                "dependency": name,
                "version": version,
                "id": str(vuln.get("id", "unknown")),
                "aliases": [str(alias) for alias in vuln.get("aliases", [])],
                "fix_versions": fixes,
                "fixable": bool(fixes),
            }
            findings.append(item)
            if fixes:
                fixable.append(item)
    return {
        "finding_count": len(findings),
        "fixable_count": len(fixable),
        "unfixed_count": len(findings) - len(fixable),
        "findings": findings,
        "fixable": fixable,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="评估 pip-audit JSON 报告")
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    try:
        data = json.loads(args.report.read_text(encoding="utf-8"))
        result = evaluate(data)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"pip-audit 报告无效：{exc}", file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["fixable_count"]:
        print("发现已有修复版本的依赖漏洞，必须升级后再合并。", file=sys.stderr)
        return 1
    if result["unfixed_count"]:
        print("仅发现尚无修复版本的上游漏洞；报告保留为审计证据。")
    else:
        print("未发现已知依赖漏洞。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
