#!/usr/bin/env python3
"""Bootstrap the isolated self-upgrade runner with trusted diff-aware redlines."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("AGENELF_ROOT", Path(__file__).resolve().parents[1])).resolve()
APP_DIR = ROOT / "app-fork"
SCRIPT_DIR = Path(__file__).resolve().parent
for entry in (APP_DIR, SCRIPT_DIR):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from core import authorized_upgrade, upgrade_redlines  # noqa: E402

upgrade_redlines.install(authorized_upgrade)

from self_upgrade_runner import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
