"""Agenelf core package initialization.

Keep ordinary core imports lightweight. Existing consumers import
``authorized_upgrade`` from this package, so expose a lazy module proxy. The proxy loads
the complete legacy engine only on first use, then installs Node scopes and the final
diff-aware redline scanner before forwarding the requested attribute.
"""
from __future__ import annotations

import importlib
import re
from types import ModuleType
from typing import Any

_AUTHORIZED_UPGRADE: ModuleType | None = None


def load_authorized_upgrade() -> ModuleType:
    global _AUTHORIZED_UPGRADE
    if _AUTHORIZED_UPGRADE is not None:
        return _AUTHORIZED_UPGRADE

    module = importlib.import_module(f"{__name__}.authorized_upgrade")

    from .node_upgrade_policy import install as install_node_policy
    from .read_ops_upgrade_policy import install as install_read_ops_policy
    from .upgrade_redlines import install as install_diff_redlines

    # Import and install extensions only inside the real upgrade entrypoint. Ordinary
    # Agent, Approval and Runner imports remain lightweight and side-effect free.
    install_node_policy()
    module.NODE_UPGRADE_POLICY_VERSION = "owner-authorized-node-upgrade-v1"
    install_read_ops_policy(module)
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


class _AuthorizedUpgradeProxy(ModuleType):
    def __init__(self) -> None:
        super().__init__(f"{__name__}.authorized_upgrade_proxy")

    def __getattr__(self, name: str) -> Any:
        return getattr(load_authorized_upgrade(), name)


# The attribute exists before from-list processing, preventing Python from importing the
# raw submodule and bypassing governance installation.
authorized_upgrade = _AuthorizedUpgradeProxy()

__all__ = ["authorized_upgrade", "load_authorized_upgrade"]
