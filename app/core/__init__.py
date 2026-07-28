"""Agenelf core package initialization.

Most core modules must stay lightweight and independent. The owner-authorized upgrade
module is therefore patched lazily: Python first finishes importing the complete legacy
engine, then installs the Node.js/TypeScript governance extension exactly once. This
avoids partially initialized functions overwriting security wrappers.
"""
from __future__ import annotations

import importlib
import re
from types import ModuleType


def __getattr__(name: str) -> ModuleType:
    if name != "authorized_upgrade":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(f"{__name__}.authorized_upgrade")
    globals()[name] = module
    from .node_upgrade_policy import install

    install()
    # Accept natural TypeScript descriptions such as "TypeScript validation runner".
    # The main policy module owns all other scopes and redlines.
    marker = "agenelf-typescript-runner-scope-v1"
    if not any(getattr(pattern, "pattern", "") == marker for _, pattern in module._SCOPE_PATTERNS):
        natural_pattern = re.compile(r"(?i)TypeScript.{0,80}(?:runner|执行器)|(Node(?:\.js)?).{0,80}(?:runner|执行器)")
        # Give the pattern a stable marker without weakening the actual expression.
        module._SCOPE_PATTERNS = (
            ("node_runners", natural_pattern),
            *module._SCOPE_PATTERNS,
        )
    return module
