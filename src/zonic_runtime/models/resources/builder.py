"""``resources/builder-registry.json``: what the Strands builder knows about node types (plan P1-05).

Vendored by ``scripts/sync_builder.py`` from ``backend/workflow/resources`` and checked for
drift in CI: the node semantics (entry type, containers, branches, handoffs, child
references), each node type's handles, output schema and default configuration, the option
sets, and the builder's blueprint rules (the Swarm rules SW-1 to SW-12). The blueprint reads
node roles from here, so it never names a node type (design rule 1).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from zonic_runtime.models.base import ZonicResourceModel
from zonic_runtime.models.registry import exported

RuleCheckKind = Literal[
    "non_empty_list",
    "required",
    "forbidden",
    "refs_exist",
    "unique_by",
    "one_of_refs",
    "exclusive_refs",
    "no_edges_to_refs",
    "each_ref",
    "compare",
]


class BranchSemantics(ZonicResourceModel):
    path: str
    id_field: str


class SwarmSemantics(ZonicResourceModel):
    members_path: str
    entry_point_path: str
    member_types: list[str]


class NodeSemantics(ZonicResourceModel):
    """Structural roles by node type; paths are dot paths from the node root."""

    child_reference: dict[str, str] = Field(default_factory=dict)
    handoff_targets: dict[str, str] = Field(default_factory=dict)
    branches: dict[str, BranchSemantics] = Field(default_factory=dict)
    entry: dict[str, bool] = Field(default_factory=dict)
    swarms: dict[str, SwarmSemantics] = Field(default_factory=dict)


class RuleCheck(ZonicResourceModel):
    """The nested check of an ``each_ref`` rule, run on every node the list names."""

    rule: RuleCheckKind
    path: str | None = None


class RuleCompare(ZonicResourceModel):
    left: str | list[str]
    op: str
    right: Any = None


class BuilderRule(ZonicResourceModel):
    """One builder validation rule with an ID; messages use the builder's wording."""

    id: str
    rule: RuleCheckKind
    severity: Literal["error", "warning"]
    message: str
    path: str | None = None
    field: str | None = None
    refs_path: str | None = None
    allowed_types_from: str | None = None
    check: RuleCheck | None = None
    left: str | list[str] | None = None
    op: str | None = None
    right: Any = None
    when: RuleCompare | None = None


class BuilderNodeType(ZonicResourceModel):
    source_handles: list[str] = Field(default_factory=list)
    target_handles: list[str] = Field(default_factory=list)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    default_config: dict[str, Any] = Field(default_factory=dict)


@exported("resources")
class BuilderRegistry(ZonicResourceModel):
    schema_version: str
    builder_commit: str
    semantics: NodeSemantics
    condition_operators: list[str]
    condition_logic: list[str]
    state_ops: list[str]
    mapping_types: list[str]
    #: Node type -> its rules with an ID (the blueprint checks; the others are form hints).
    validation_rules: dict[str, list[BuilderRule]]
    node_types: dict[str, BuilderNodeType]
    #: Builder source file -> SHA-256 of its text (LF line ends).
    sources: dict[str, str]
