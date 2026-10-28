"""The trace and usage API's responses (design 12.2; plan P3-11), the Node.js runtime's shapes."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from zonic_runtime.models.base import ZonicModel


class TraceScore(ZonicModel):
    name: str
    data_type: str
    value: float | None = None
    string_value: str | None = None
    source: str | None = None
    comment: str | None = None
    observation_id: str | None = None
    timestamp: str | None = None


class TraceObservation(ZonicModel):
    observation_id: str
    parent_observation_id: str | None = None
    type: str
    name: str
    level: str = "DEFAULT"
    status_message: str | None = None
    start_time: str = ""
    end_time: str | None = None
    latency_ms: float | None = None
    model: str | None = None
    usage: dict[str, Any] | None = None
    total_cost: float | None = None
    node_id: str | None = None
    node_type: str | None = None
    node_name: str | None = None
    visit: int | None = None
    path: str | None = None
    input: Any = None
    output: Any = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    #: Nested by ``parentObservationId``, each level in start order.
    children: list[TraceObservation] = Field(default_factory=list)


class Trace(ZonicModel):
    """``GET /v1/runs/{runId}/trace``: the trace header, its observation tree and its scores."""

    trace_id: str
    run_id: str | None = None
    name: str | None = None
    status: str | None = None
    session_id: str | None = None
    agentic_workflow_id: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    latency_ms: float | None = None
    observation_count: int = 0
    error_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_cost: float = 0
    tags: list[str] = Field(default_factory=list)
    observations: list[TraceObservation] = Field(default_factory=list)
    scores: list[TraceScore] = Field(default_factory=list)


class TraceLink(ZonicModel):
    """``GET /v1/runs/{runId}/trace-link``: the run's trace on the Langfuse server, when one is configured."""

    trace_id: str
    url: str


class UsageItem(ZonicModel):
    day: str
    agentic_workflow_id: str | None = None
    node_type: str | None = None
    node_name: str | None = None
    model: str | None = None
    executions: int | None = None
    errors: int | None = None
    error_rate: float | None = None
    avg_latency_ms: float | None = None
    p95_latency_ms: float | None = None
    calls: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_cost: float = 0


class UsageList(ZonicModel):
    """``GET /v1/observability/usage``: daily node usage, or model usage of the caller's tenant."""

    group_by: Literal["node", "model"]
    items: list[UsageItem] = Field(default_factory=list)
