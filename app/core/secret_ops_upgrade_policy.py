"""Owner-authorized upgrade scope extension for isolated environment Secret Ops."""
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
    module._ALLOWED_BASENAMES.add("Dockerfile.ops-secret")
    module._SCOPE_PATHS["node_runners"] = _extend(
        module._SCOPE_PATHS.get("node_runners", ()),
        "node/apps/secret-ops-runner/",
        "node/apps/secret-cli/",
        "node/packages/core/src/secret-ops.ts",
        "node/packages/core/src/secret-env.ts",
        "node/packages/core/src/secret-targets.ts",
    )
    module._SCOPE_PATHS["node_build"] = _extend(
        module._SCOPE_PATHS.get("node_build", ()),
        "Dockerfile.ops-secret",
    )
    module._SCOPE_PATHS["compose"] = _extend(
        module._SCOPE_PATHS.get("compose", ()),
        "compose.override.yaml",
        "Dockerfile.ops-secret",
    )
    pattern = re.compile(
        r"(?i)(?:secret|credential|密钥|凭据).{0,100}(?:env|ops|SSH|runner|console|执行器|控制台)|"
        r"node/apps/secret-(?:ops-runner|cli)|node/packages/core/src/secret-(?:ops|env|targets)"
    )
    if not any(scope == "node_runners" and item.pattern == pattern.pattern for scope, item in module._SCOPE_PATTERNS):
        module._SCOPE_PATTERNS = (("node_runners", pattern), *module._SCOPE_PATTERNS)
    module.SECRET_OPS_UPGRADE_POLICY_VERSION = "owner-authorized-secret-ops-v1"
    _INSTALLED = True
