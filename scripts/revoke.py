#!/usr/bin/env python3
"""Cross-platform owner command for cancelling an operation before it starts.

Examples:
  python scripts/revoke.py op-0123456789abcdef
  py -3 scripts\revoke.py latest "任务范围已改变" --as sirius

The command imports trusted ``app/`` source first, acquires the same per-request lock as
``ops-runner`` and writes a ``cancelled`` result only when execution has not started.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_APP = ROOT / "app"
RUNTIME_APP = ROOT / "app-fork"
APP_DIR = SOURCE_APP if SOURCE_APP.is_dir() else RUNTIME_APP
if not APP_DIR.is_dir():
    raise SystemExit(f"Agenelf app source not found: {SOURCE_APP} or {RUNTIME_APP}")
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from core import operation_revocation  # noqa: E402


def _default_actor() -> str:
    raw = os.environ.get("USERNAME") or os.environ.get("USER") or "owner"
    cleaned = re.sub(r"[^A-Za-z0-9._@:-]", "-", str(raw))[:64].strip("-")
    return f"host:{cleaned or 'owner'}"


def _resolve(value: str) -> str:
    raw = str(value or "").strip().lower()
    if raw not in {"latest", "newest", "最新", "刚才"}:
        return raw
    rows = operation_revocation.list_revocable_operations(root=ROOT, limit=20)
    if not rows:
        raise operation_revocation.OperationRevocationError("当前没有可撤销的运维请求")
    return str(rows[0]["id"])


def main() -> int:
    parser = argparse.ArgumentParser(
        description="在 ops-runner 开始前原子撤销一个 Agenelf 运维请求"
    )
    parser.add_argument("request_id", help="op-... / latest")
    parser.add_argument("reason", nargs="?", default="")
    parser.add_argument("--as", dest="cancelled_by", default="")
    args = parser.parse_args()
    actor = args.cancelled_by.strip() or _default_actor()
    try:
        request_id = _resolve(args.request_id)
        result = operation_revocation.revoke_operation(
            request_id,
            args.reason,
            actor,
            root=ROOT,
        )
    except operation_revocation.OperationRevocationError as exc:
        print(
            json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2),
            file=sys.stderr,
        )
        return 2
    print(json.dumps({"ok": True, "revocation": result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
