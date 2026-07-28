"""Agenelf core package initialization.

Most core modules must stay lightweight and independent. The owner-authorized upgrade
module is patched lazily: Python first finishes importing the complete legacy engine,
then installs the Node.js/TypeScript scope extension and the final language-neutral,
diff-aware redline scanner exactly once.
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

    from .node_upgrade_policy import install as install_node_policy
    from .upgrade_redlines import install as install_diff_redlines

    install_node_policy()
    install_diff_redlines(module)

    # Accept natural descriptions such as "TypeScript validation runner" in addition
    # to the stricter scope patterns maintained by node_upgrade_policy.py.
    natural_pattern = re.compile(
        r"(?i)TypeScript.{0,80}(?:runner|执行器)|"
        r"Node(?:\.js)?.{0,80}(?:runner|执行器)"
    )
    if not any(
        scope == "node_runners" and pattern.pattern == natural_pattern.pattern
        for scope, pattern in module._SCOPE_PATTERNS
    ):
        module._SCOPE_PATTERNS = (
            ("node_runners", natural_pattern),
            *module._SCOPE_PATTERNS,
        )
    return module
