#!/usr/bin/env python3
"""Lifecycle-aware entrypoint for the unified deterministic SSH runner.

The existing unified runner remains responsible for schema, fingerprint, policy,
approval, allowlist and SSH validation. This entrypoint adds one fail-closed step:
expired operation requests are finalized without opening an SSH connection.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from ops_runner import _atomic_json, _read_json, now_iso  # noqa: E402
from unified_ops_runner import UnifiedOpsRunner, _sanitize_text  # noqa: E402
from core import operations  # noqa: E402


class LifecycleOpsRunner(UnifiedOpsRunner):
    """Close expired requests before any server profile or SSH work is attempted."""

    def _expire_request(
        self,
        request: dict[str, Any],
        result_path: Path,
    ) -> str:
        request_id = str(request.get("id", ""))
        expiry = operations.request_expiry(request)
        result = {
            "schema_version": 1,
            "id": request_id,
            "status": "expired",
            "capability": str(request.get("capability", "")),
            "operation": str(request.get("operation", "")),
            "target": str(request.get("target", "")),
            "reason": "操作请求已超过绑定有效期，未连接服务器；请重新提交以生成新请求",
            "expired_at": expiry.isoformat(timespec="seconds") if expiry else None,
            "finished_at": now_iso(),
            "commands": [],
        }
        try:
            _atomic_json(result_path, result, exclusive=True)
        except FileExistsError:
            return "done"
        self.audit("expired", f"{request_id} request_ttl_elapsed")
        return "expired"

    def process_request(self, request_path: Path) -> str:
        request = _read_json(request_path)
        if request is None:
            return "invalid"
        request_id = str(request.get("id", ""))
        result_path = self.paths["results"] / f"{request_id}.json"
        if result_path.exists():
            return "done"
        if not operations.request_expired(request, fail_closed=True):
            return super().process_request(request_path)

        lock_path = self.paths["locks"] / f"{request_id}.lock"
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        except FileExistsError:
            return "locked"
        try:
            if result_path.exists():
                return "done"
            return self._expire_request(request, result_path)
        finally:
            try:
                lock_path.unlink()
            except OSError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Agenelf lifecycle-aware unified SSH operations runner"
    )
    parser.add_argument("--once", action="store_true", help="process queue once and exit")
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    try:
        runner = LifecycleOpsRunner()
        if args.once:
            print(json.dumps(runner.run_once(), ensure_ascii=False))
        else:
            runner.watch(args.interval)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(
            f"ops-runner entry 启动失败：{type(exc).__name__}: {_sanitize_text(exc)}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
