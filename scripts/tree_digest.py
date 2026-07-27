#!/usr/bin/env python3
"""Compute a deterministic SHA-256 digest for a candidate source tree."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

_EXCLUDED_DIRS = {"__pycache__", ".pytest_cache"}
_EXCLUDED_SUFFIXES = {".pyc", ".pyo"}
# gate 的暂存请求队列（PROMOTE_REQUESTS_DIR 默认 app-tmp/promote-requests）在
# 扁平布局下位于候选树顶层；它是流程产物而非候选代码，只对顶层条目豁免，
# 更深层级的同名目录仍参与摘要，防止模型借此藏匿代码。
_EXCLUDED_TOP_LEVEL = {"promote-requests"}


def tree_digest(root: str | Path) -> str:
    base = Path(root).resolve()
    if not base.is_dir():
        raise ValueError(f"候选目录不存在：{base}")
    digest = hashlib.sha256()
    files: list[Path] = []
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(base)
        if relative.parts[0] in _EXCLUDED_TOP_LEVEL:
            continue
        if any(part in _EXCLUDED_DIRS for part in relative.parts):
            continue
        if path.suffix in _EXCLUDED_SUFFIXES:
            continue
        files.append(path)
    for path in sorted(files, key=lambda item: item.relative_to(base).as_posix()):
        relative = path.relative_to(base).as_posix().encode("utf-8")
        size = path.stat().st_size
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(size.to_bytes(8, "big"))
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="计算候选代码树摘要")
    parser.add_argument("root", help="要计算摘要的目录")
    args = parser.parse_args()
    try:
        print(tree_digest(args.root))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
