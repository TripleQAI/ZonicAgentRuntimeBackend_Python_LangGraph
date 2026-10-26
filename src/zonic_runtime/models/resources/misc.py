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
    path: str  # selector path, e.g. 'nodes[type=rule].config.rules[*].conditions[*].operator'
    equals: Any = None
    in_: list[Any] | None = Field(default=None, alias="in")
    exists: bool | None = None


class DialectRule(ZonicResourceModel):
    id: str
    match: DialectMatch
    set: dict[str, Any] = Field(default_factory=dict)
    rename: dict[str, str] = Field(default_factory=dict)
    remove: list[str] = Field(default_factory=list)
    transform: str | None = None  # a named transform registered in the normalizer
    note: str


@exported("resources")
class DialectRules(ZonicResourceModel):
    schema_version: str
    rules: list[DialectRule]
