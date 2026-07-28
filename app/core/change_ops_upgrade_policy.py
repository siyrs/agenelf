"""Owner-authorized upgrade scope extension for governed Node change/privileged Ops."""
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
    module._ALLOWED_BASENAMES.add("Dockerfile.ops-change")
    module._SCOPE_PATHS["node_runners"] = _extend(
        module._SCOPE_PATHS.get("node_runners", ()),
        "node/apps/change-ops-runner/",
        "node/packages/core/src/change-ops.ts",
        "node/packages/core/src/open-ssh.ts",
        "node/packages/core/src/server-catalog.ts",
    )
    module._SCOPE_PATHS["node_build"] = _extend(
        module._SCOPE_PATHS.get("node_build", ()),
        "Dockerfile.ops-change",
    )
    module._SCOPE_PATHS["compose"] = _extend(
        module._SCOPE_PATHS.get("compose", ()),
        "compose.override.yaml",
        "Dockerfile.ops-change",
    )
    pattern = re.compile(
        r"(?i)(?:change|privileged|变更|高权限).{0,80}(?:ops|operations|SSH|runner|执行器)|"
        r"node/apps/change-ops-runner|node/packages/core/src/(?:change-ops|open-ssh|server-catalog)"
    )
    if not any(scope == "node_runners" and item.pattern == pattern.pattern for scope, item in module._SCOPE_PATTERNS):
        module._SCOPE_PATTERNS = (("node_runners", pattern), *module._SCOPE_PATTERNS)
    module.CHANGE_OPS_UPGRADE_POLICY_VERSION = "owner-authorized-change-ops-v1"
    _INSTALLED = True
