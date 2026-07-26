#!/usr/bin/env python3
"""Cross-platform host CLI for exact Agenelf owner decisions.

Examples:
  python scripts/approve.py op-0123456789abcdef approve
  py -3 scripts\\approve.py latest approve --as sirius
  python scripts/approve.py op-0123456789abcdef deny "暂不执行"
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / ("app-fork" if (ROOT / "app-fork").is_dir() else "app")
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from core import owner_approval  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Agenelf 跨平台精确审批工具")
    parser.add_argument("request_id", help="op-... / auth-... / latest")
    parser.add_argument("action", nargs="?", default="approve", choices=["approve", "deny"])
    parser.add_argument("reason", nargs="?", default="")
    parser.add_argument("--as", dest="decided_by", default="")
    args = parser.parse_args()
    actor = args.decided_by.strip() or owner_approval.default_actor().removeprefix("cli:")
    try:
        requested = str(args.request_id).strip()
        if requested.lower() in {"latest", "newest"} or requested in {"最新", "刚才"}:
            selected, _duplicates = owner_approval.resolve_pending_operation(root=ROOT)
            requested = str(selected["id"])
        result = owner_approval.apply_owner_decision(
            requested,
            args.action,
            args.reason,
            actor,
            root=ROOT,
        )
    except owner_approval.ApprovalError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, "decision": result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
