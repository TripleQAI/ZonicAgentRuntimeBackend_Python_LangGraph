"""Models of the smaller resource files.

``observability.json``, ``error-catalog.json``, ``templates.json``, ``run-states.json`` and ``dialects.json``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from zonic_runtime.models.base import ZonicResourceModel
from zonic_runtime.models.registry import exported

from .manifest import Capture


class CaptureRule(ZonicResourceModel):
    input: Capture
    output: Capture


class MaskRules(ZonicResourceModel):
    keys: list[str]  # case-insensitive substrings of keys whose values are always masked
    patterns: dict[str, str]  # name -> regular expression applied to text
    replacement: str


class ObservationLimits(ZonicResourceModel):
    max_field_bytes: int = Field(gt=0)
    max_observation_bytes: int = Field(gt=0)


@exported("resources")
class ObservabilityConfig(ZonicResourceModel):
    capture_by_node_type: dict[str, CaptureRule]
    mask: MaskRules
    limits: ObservationLimits
    genai_attribute_map: dict[str, str]  # gen_ai.* attribute -> Langfuse record field


class ErrorEntry(ZonicResourceModel):
    status: int = Field(ge=100, lt=600)
    retryable: bool
    message: str  # user-safe text; the only place user-facing English lives (rule 6)


@exported("resources")
class ErrorCatalog(ZonicResourceModel):
    errors: dict[str, ErrorEntry]

    @model_validator(mode="after")
    def _codes_are_upper_snake(self) -> ErrorCatalog:
        bad = [c for c in self.errors if not c.replace("_", "").isalnum() or c.upper() != c]
        if bad:
            raise ValueError(f"error codes must be UPPER_SNAKE: {bad}")
        return self


@exported("resources")
class TemplateNamespaces(ZonicResourceModel):
    namespaces: list[str]
    aliases: dict[str, str]  # alias namespace -> namespace (node -> nodes, variable -> flow)
    special: dict[str, str]  # special roots (nodeOutput, runtime.workflowMetaData, ...) -> resolver key
    bare_names: Literal["flow", "error"]  # how a bare {{name}} resolves


@exported("resources")
class RunStateMachine(ZonicResourceModel):
    states: list[str]
    initial: dict[str, str]  # run mode -> first state
    terminal: list[str]
    transitions: dict[str, list[str]]

    @model_validator(mode="after")
    def _closed(self) -> RunStateMachine:
        known = set(self.states)
        refs = set(self.terminal) | set(self.initial.values()) | set(self.transitions)
        refs |= {t for targets in self.transitions.values() for t in targets}
        unknown = sorted(refs - known)
        if unknown:
            raise ValueError(f"run states referenced but not declared: {unknown}")
        return self


class DialectMatch(ZonicResourceModel):
    """When a rule applies to an item (document, node or edge); every given test must hold."""

    has: str | None = None  # the path exists
    non_empty: str | None = None  # the path holds a non-empty list
    equals: dict[str, Any] = Field(default_factory=dict)  # path -> value
    type_of: dict[str, str] = Field(default_factory=dict)  # path -> JSON type name


class DialectRule(ZonicResourceModel):
    """One normalization rule (design 5.2): a generic operation with its arguments and note."""

    id: str
    rule_class: Literal["alias", "platform"] = Field(alias="class")
    target: Literal["document", "node", "edge"]
    op: str
    args: dict[str, Any] = Field(default_factory=dict)
    when: DialectMatch | None = None
    note: str
    node_types: list[str] = Field(default_factory=list)  # empty with kinds empty: every node
    kinds: list[str] = Field(default_factory=list)


@exported("resources")
class DialectRules(ZonicResourceModel):
    """``dialects.json``: everything the normalizer knows about the two dialects."""

    schema_version: str
    layout_keys: list[str] = Field(default_factory=list)
    edge_layout_keys: list[str] = Field(default_factory=list)
    ignored_document_fields: list[str] = Field(default_factory=list)
    ignored_config_fields: dict[str, list[str]] = Field(default_factory=dict)  # node type -> config keys
    canonical_operators: list[str] = Field(default_factory=list)
    operator_aliases: dict[str, str] = Field(default_factory=dict)
    default_rule_ids: list[str] = Field(default_factory=list)
    agent_strategies: list[str] = Field(default_factory=list)
    rules: list[DialectRule]

    @model_validator(mode="after")
    def _consistent(self) -> DialectRules:
        ids = [r.id for r in self.rules]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"duplicate dialect rule IDs: {duplicates}")
        unknown = sorted(set(self.operator_aliases.values()) - set(self.canonical_operators))
        if unknown:
            raise ValueError(f"operator aliases point at unknown operators: {unknown}")
        return self
