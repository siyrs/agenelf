"""Owner-authorized scope extension for the Node Self-upgrade Runner body."""
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
    module._SCOPE_PATHS["node_runners"] = _extend(
        module._SCOPE_PATHS.get("node_runners", ()),
        "node/apps/self-upgrade-runner/",
        "node/packages/core/src/self-upgrade.ts",
        "node/packages/core/src/self-upgrade-hardening.ts",
    )
    module._SCOPE_PATHS["node_build"] = _extend(
        module._SCOPE_PATHS.get("node_build", ()),
        "Dockerfile.control-plane",
    )
    module._SCOPE_PATHS["compose"] = _extend(
        module._SCOPE_PATHS.get("compose", ()),
        "compose.override.yaml",
        "Dockerfile.control-plane",
    )
    pattern = re.compile(
        r"(?i)self[-_ ]?upgrade.{0,80}(?:Node|TypeScript|runner|执行器)|"
        r"node/apps/self-upgrade-runner|node/packages/core/src/self-upgrade(?:-hardening)?"
    )
    if not any(scope == "node_runners" and item.pattern == pattern.pattern for scope, item in module._SCOPE_PATTERNS):
        module._SCOPE_PATTERNS = (("node_runners", pattern), *module._SCOPE_PATTERNS)
    module.SELF_UPGRADE_RUNTIME_POLICY_VERSION = "owner-authorized-self-upgrade-runtime-v1"
    _INSTALLED = True
