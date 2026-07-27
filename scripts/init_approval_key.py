#!/usr/bin/env python3
"""Create a persistent Docker-volume key for the owner approval command channel."""
from __future__ import annotations

import os
import secrets
import tempfile
from pathlib import Path


def initialize(path: Path) -> dict[str, object]:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and len(path.read_bytes().strip()) >= 32:
        return {"created": False, "path": str(path), "bytes": path.stat().st_size}
    fd, temp_name = tempfile.mkstemp(prefix=".approval-key-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(secrets.token_urlsafe(48).encode("ascii") + b"\n")
        os.chmod(temp_name, 0o444)
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return {"created": True, "path": str(path), "bytes": path.stat().st_size}


def main() -> int:
    path = Path(os.environ.get("AGENELF_APPROVAL_KEY_FILE", "/agenelf/approval/key"))
    result = initialize(path)
    print(
        "approval key ready: "
        f"created={str(result['created']).lower()} bytes={result['bytes']} path={result['path']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
