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
    module._ALLOWED_BASENAMES.update(
        {
            "Dockerfile.ops-secret",
            "compose.secret-chat.yaml",
        }
    )
    module._SCOPE_PATHS["node_runners"] = _extend(
        module._SCOPE_PATHS.get("node_runners", ()),
        "node/apps/secret-ops-runner/",
        "node/apps/secret-cli/",
        "node/apps/secret-chat-broker/",
        "node/packages/core/src/secret-ops.ts",
        "node/packages/core/src/secret-env.ts",
        "node/packages/core/src/secret-targets.ts",
    )
    module._SCOPE_PATHS["node_runtime"] = _extend(
        module._SCOPE_PATHS.get("node_runtime", ()),
        "node/packages/core/src/chat-secret-env.ts",
        "node/packages/core/src/secret-chat-client.ts",
        "node/packages/core/src/secret-chat-direct.ts",
        "node/packages/core/src/agent-events.ts",
        "node/packages/core/src/agent.ts",
        "node/packages/core/src/types.ts",
    )
    module._SCOPE_PATHS["node_skills"] = _extend(
        module._SCOPE_PATHS.get("node_skills", ()),
        "node/packages/skills/src/builtin.ts",
    )
    module._SCOPE_PATHS["node_build"] = _extend(
        module._SCOPE_PATHS.get("node_build", ()),
        "Dockerfile.ops-secret",
    )
    module._SCOPE_PATHS["compose"] = _extend(
        module._SCOPE_PATHS.get("compose", ()),
        "compose.yaml",
        "compose.override.yaml",
        "compose.secret-chat.yaml",
        "Dockerfile.ops-secret",
    )
    pattern = re.compile(
        r"(?i)(?:secret|credential|密钥|凭据).{0,100}(?:env|ops|SSH|runner|console|chat|broker|route|event|执行器|控制台|聊天|路由|事件)|"
        r"node/apps/secret-(?:ops-runner|cli|chat-broker)|"
        r"node/packages/core/src/(?:secret-(?:ops|env|targets|chat-client|chat-direct)|chat-secret-env|agent-events)"
    )
    if not any(scope == "node_runners" and item.pattern == pattern.pattern for scope, item in module._SCOPE_PATTERNS):
        module._SCOPE_PATTERNS = (("node_runners", pattern), *module._SCOPE_PATTERNS)
    module.SECRET_OPS_UPGRADE_POLICY_VERSION = "owner-authorized-secret-ops-v3"
    _INSTALLED = True
