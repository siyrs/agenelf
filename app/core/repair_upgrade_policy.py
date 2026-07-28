"""Owner-authorized upgrade scope extension for the Node Repair Runner."""
from __future__ import annotations

import re
from types import ModuleType

_INSTALLED = False


def _extend(values: tuple[str, ...], *items: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*values, *items)))


def install(module: ModuleType) -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    module._ALLOWED_BASENAMES.add("Dockerfile.repair")
    module._SCOPE_PATHS["node_runners"] = _extend(
        module._SCOPE_PATHS.get("node_runners", ()),
        "node/apps/repair-runner/",
        "node/packages/core/src/repair.ts",
    )
    module._SCOPE_PATHS["node_build"] = _extend(
        module._SCOPE_PATHS.get("node_build", ()),
        "Dockerfile.repair",
    )
    module._SCOPE_PATHS["compose"] = _extend(
        module._SCOPE_PATHS.get("compose", ()),
        "compose.override.yaml",
        "Dockerfile.repair",
    )
    pattern = re.compile(
        r"(?i)(?:repair|修复).{0,80}(?:runner|执行器|isolated|worktree)|"
        r"node/apps/repair-runner|node/packages/core/src/repair"
    )
    if not any(scope == "node_runners" and item.pattern == pattern.pattern for scope, item in module._SCOPE_PATTERNS):
        module._SCOPE_PATTERNS = (("node_runners", pattern), *module._SCOPE_PATTERNS)
    module.REPAIR_UPGRADE_POLICY_VERSION = "owner-authorized-node-repair-v1"
    _INSTALLED = True
