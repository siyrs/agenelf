"""Owner-authorized upgrade scope extension for the Node read-only Ops runtime."""
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
    module._ALLOWED_BASENAMES.add("Dockerfile.ops-read")
    module._SCOPE_PATHS["node_runners"] = _extend(
        module._SCOPE_PATHS.get("node_runners", ()),
        "node/apps/read-ops-runner/",
        "node/packages/core/src/read-ops.ts",
        "node/packages/core/src/server-catalog.ts",
    )
    module._SCOPE_PATHS["node_build"] = _extend(
        module._SCOPE_PATHS.get("node_build", ()),
        "Dockerfile.ops-read",
    )
    module._SCOPE_PATHS["compose"] = _extend(
        module._SCOPE_PATHS.get("compose", ()),
        "compose.override.yaml",
        "Dockerfile.ops-read",
    )
    pattern = re.compile(
        r"(?i)(?:read.?only|只读).{0,80}(?:ops|operations|SSH|runner|执行器)|"
        r"node/apps/read-ops-runner|node/packages/core/src/(?:read-ops|server-catalog)"
    )
    if not any(scope == "node_runners" and item.pattern == pattern.pattern for scope, item in module._SCOPE_PATTERNS):
        module._SCOPE_PATTERNS = (("node_runners", pattern), *module._SCOPE_PATTERNS)
    module.READ_OPS_UPGRADE_POLICY_VERSION = "owner-authorized-read-ops-v1"
    _INSTALLED = True
