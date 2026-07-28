"""Agenelf core package initialization.

The owner-authorized upgrade engine remains Python during migration. Install the
Node.js/TypeScript governance extension once at package import so the Agent and the
isolated self-upgrade Runner share the exact same scopes, redlines and regression rules.
"""
from __future__ import annotations

from .node_upgrade_policy import install as _install_node_upgrade_policy

_install_node_upgrade_policy()
del _install_node_upgrade_policy
