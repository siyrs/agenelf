"""Agenelf core package initialization.

The owner-authorized upgrade engine is still the shared Python control plane during the
Node migration. Load that module explicitly to completion, register the module object on
the package, and only then install Node scopes and the final diff-aware redline scanner.
All existing ``from core import authorized_upgrade`` consumers therefore receive the
same fully governed module without relying on import-order side effects.
"""
from __future__ import annotations

import importlib
import re
from types import ModuleType

_AUTHORIZED_UPGRADE: ModuleType | None = None


def load_authorized_upgrade() -> ModuleType:
    global _AUTHORIZED_UPGRADE
    if _AUTHORIZED_UPGRADE is not None:
        return _AUTHORIZED_UPGRADE

    module = importlib.import_module(f"{__name__}.authorized_upgrade")
    globals()["authorized_upgrade"] = module

    from .node_upgrade_policy import install as install_node_policy
    from .upgrade_redlines import install as install_diff_redlines

    install_node_policy()
    install_diff_redlines(module)

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

    _AUTHORIZED_UPGRADE = module
    return module


authorized_upgrade = load_authorized_upgrade()

__all__ = ["authorized_upgrade", "load_authorized_upgrade"]
