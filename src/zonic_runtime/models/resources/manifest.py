"""``resources/node-runtime.json``: the only place node types meet node kinds (design Section 8.3)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from zonic_runtime.models.base import ZonicResourceModel
from zonic_runtime.models.io.ports import NodeIoSpec
from zonic_runtime.models.registry import exported

# Langfuse's observation vocabulary, which shares some words with node type names.
ObservationType = Literal[
    "span", "generation", "event", "agent", "tool", "chain", "retriever", "evaluator", "embedding", "guardrail"  # principle: allow rule-1-node-type langfuse-observation-types  # noqa: E501
]  # fmt: skip
Capture = Literal["full", "summary", "none"]
JoinPolicy = Literal["auto", "any", "barrier"]
ErrorPolicy = Literal["fail_run", "continue", "route"]


class ObservationSpec(ZonicResourceModel):
    type: ObservationType
    capture_input: Capture
    capture_output: Capture


class RetrySpec(ZonicResourceModel):
    """Becomes a LangGraph ``RetryPolicy``; ``on`` lists retryable error-catalog codes."""

    max_attempts: int = Field(ge=1)
    initial_interval_seconds: float | None = Field(default=None, gt=0)
    backoff_factor: float | None = Field(default=None, gt=0)
    max_interval_seconds: float | None = Field(default=None, gt=0)
    jitter: bool | None = None
    on: list[str] = Field(default_factory=list)
    methods: list[str] = Field(default_factory=list)  # http kind: methods that may be retried


class NodeTypeDefaults(ZonicResourceModel):
    timeout_seconds: float = Field(gt=0)
    idle_timeout_seconds: float | None = Field(default=None, gt=0)
    error_policy: ErrorPolicy
    join_policy: JoinPolicy
    default_handle: str
    error_handle: str  # activated under error policy ``route``
    handoff_handle_prefix: str  # handle of a handoff to node X is ``<prefix>X``
    retry: RetrySpec
    observation: ObservationSpec


class NodeTypeEntry(ZonicResourceModel):
    kind: str
    options: dict[str, Any] = Field(default_factory=dict)
    handles: dict[str, str] = Field(default_factory=dict)  # semantic name -> builder handle ID
    optional_handles: list[str] = Field(default_factory=list)
    timeout_seconds: float | None = Field(default=None, gt=0)
    idle_timeout_seconds: float | None = Field(default=None, gt=0)
    timeout_seconds_from: str | None = None  # a config path that overrides the timeout
    error_policy: ErrorPolicy | None = None
    join_policy: JoinPolicy | None = None
    default_handle: str | None = None
    retry: RetrySpec | None = None
    stream_tokens: bool | None = None
    terminal: bool = False
    enabled_by: str | None = None  # a feature flag in settings.features
    observation: ObservationSpec | None = None
    io: NodeIoSpec | None = None


@exported("resources")
class NodeRuntimeManifest(ZonicResourceModel):
    schema_version: str
    defaults: NodeTypeDefaults
    on_unknown_type: Literal["fail_plan", "ignore"]
    #: Schemas by registry ID (``port.text@1``, ``ext.view.html@1``): port schemas and output extensions.
    schema_extensions: dict[str, dict[str, Any]] = Field(default_factory=dict)
    node_types: dict[str, NodeTypeEntry]

    @model_validator(mode="after")
    def _kinds_present(self) -> NodeRuntimeManifest:
        missing = [t for t, e in self.node_types.items() if not e.kind]
        if missing:
            raise ValueError(f"node types without a kind: {missing}")
        return self
