"""Capability metadata shared by skills, planning, and the user-facing catalog.

A capability is a coarse-grained business domain (server operations, code repair,
validation, release, ...).  A skill is still the executable Python plugin.  Keeping
those concepts separate lets Agenelf add many skills to one domain and compose
multiple domains into a workflow without changing the tool-call protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_VALID_RISKS = {"read", "change", "privileged", "forbidden"}


@dataclass(frozen=True)
class OperationDescriptor:
    """One operation exposed by a capability."""

    name: str
    description: str
    risk: str = "read"

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "OperationDescriptor":
        name = str(data.get("name", "")).strip()
        description = str(data.get("description", "")).strip()
        risk = str(data.get("risk", "read")).strip().lower()
        if not name:
            raise ValueError("capability operation name 不能为空")
        if risk not in _VALID_RISKS:
            raise ValueError(f"未知风险级别：{risk}")
        return cls(name=name, description=description, risk=risk)


@dataclass(frozen=True)
class CapabilityDescriptor:
    """Normalized capability manifest loaded from a skill module."""

    id: str
    name: str
    description: str
    version: str
    domain: str
    operations: tuple[OperationDescriptor, ...] = field(default_factory=tuple)
    composes_with: tuple[str, ...] = field(default_factory=tuple)
    source_skill: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "domain": self.domain,
            "operations": [
                {
                    "name": operation.name,
                    "description": operation.description,
                    "risk": operation.risk,
                }
                for operation in self.operations
            ],
            "composes_with": list(self.composes_with),
            "source_skill": self.source_skill,
        }


def normalize_capability_meta(
    skill_name: str,
    skill_meta: dict[str, Any] | None,
    capability_meta: dict[str, Any] | None,
    tool_names: list[str],
) -> CapabilityDescriptor:
    """Normalize optional ``CAPABILITY_META`` while preserving old skills.

    Existing skills that have no capability manifest remain loadable.  They are
    represented as a generic capability whose operations are the skill's tools.
    """

    skill_meta = skill_meta if isinstance(skill_meta, dict) else {}
    raw = capability_meta if isinstance(capability_meta, dict) else {}
    cap_id = str(raw.get("id") or skill_name).strip()
    if not cap_id:
        raise ValueError("capability id 不能为空")

    raw_operations = raw.get("operations")
    operations: list[OperationDescriptor] = []
    if isinstance(raw_operations, list):
        for item in raw_operations:
            if not isinstance(item, dict):
                raise TypeError("CAPABILITY_META.operations 每项必须是对象")
            operations.append(OperationDescriptor.from_mapping(item))
    else:
        operations.extend(
            OperationDescriptor(name=name, description="", risk="read")
            for name in tool_names
        )

    composes_with = raw.get("composes_with", [])
    if not isinstance(composes_with, list):
        raise TypeError("CAPABILITY_META.composes_with 必须是列表")

    return CapabilityDescriptor(
        id=cap_id,
        name=str(raw.get("name") or skill_meta.get("name") or skill_name),
        description=str(raw.get("description") or skill_meta.get("description") or ""),
        version=str(raw.get("version") or skill_meta.get("version") or "0.0.0"),
        domain=str(raw.get("domain") or "general"),
        operations=tuple(operations),
        composes_with=tuple(str(item) for item in composes_with if str(item).strip()),
        source_skill=skill_name,
    )
