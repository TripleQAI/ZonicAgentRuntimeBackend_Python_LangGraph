"""Langfuse record models for agent_observability (design 12; plan P2-08).

The records ``sp_obs_ingest`` takes, camelCase on the wire (``db/observability/005_procedures.sql``).
The exporter builds them as plain dictionaries on its thread; the tests validate every record it
sends against these models, and ``generated/schemas/observability/`` publishes them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import ConfigDict, Field

from zonic_runtime.models.base import ZonicModel
from zonic_runtime.models.gateway_meta import gateway_procedure, gateway_view
from zonic_runtime.models.registry import exported

ObservationType = Literal[
    "span",
    "generation",
    "event",
    "agent",  # principle: allow rule-1-node-type langfuse-observation-type
    "tool",  # principle: allow rule-1-node-type langfuse-observation-type
    "chain",
    "retriever",
    "evaluator",
    "embedding",
    "guardrail",  # principle: allow rule-1-node-type langfuse-observation-type
]
ObservationLevel = Literal["DEBUG", "DEFAULT", "WARNING", "ERROR"]
_HEX_16 = r"^[0-9a-f]{16}$"
_HEX_32 = r"^[0-9a-f]{32}$"


@exported("observability")
class ObsTraceRecord(ZonicModel):
    """A run's trace header; later values win, tags are unioned, metadata merged (O-03)."""

    trace_id: str = Field(pattern=_HEX_32)
    run_id: str | None = None
    name: str | None = Field(default=None, max_length=200)
    environment: str | None = Field(default=None, max_length=50)
    release: str | None = Field(default=None, max_length=50)
    version: str | None = Field(default=None, max_length=64)
    session_id: str | None = None
    user_id: str | None = Field(default=None, max_length=128)
    tenant_id: str | None = Field(default=None, max_length=64)
    agentic_workflow_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    input: Any = None
    output: Any = None
    status: str | None = Field(default=None, max_length=20)
    start_time: datetime | None = None
    end_time: datetime | None = None


@exported("observability")
class ObsObservationRecord(ZonicModel):
    """One node visit, model call, tool call, hook execution or event; inserted once (O-02)."""

    observation_id: str = Field(pattern=_HEX_16)
    trace_id: str = Field(pattern=_HEX_32)
    parent_observation_id: str | None = Field(default=None, pattern=_HEX_16)
    type: ObservationType
    name: str = Field(max_length=200)
    level: ObservationLevel = "DEFAULT"
    status_message: str | None = Field(default=None, max_length=500)
    environment: str | None = Field(default=None, max_length=50)
    version: str | None = Field(default=None, max_length=64)
    start_time: datetime
    end_time: datetime | None = None
    completion_start_time: datetime | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    input: Any = None
    output: Any = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    model: str | None = Field(default=None, max_length=200)
    model_parameters: dict[str, Any] | None = None
    usage_details: dict[str, Any] | None = None
    cost_details: dict[str, Any] | None = None
    #: Absent: ``sp_obs_ingest`` prices the usage from ``obs_model_price`` at ``startTime`` (O-04).
    total_cost: float | None = Field(default=None, ge=0)
    node_id: str | None = Field(default=None, max_length=100)
    node_type: str | None = Field(default=None, max_length=50)
    node_name: str | None = Field(default=None, max_length=200)
    node_kind: str | None = Field(default=None, max_length=50)
    visit: int | None = Field(default=None, ge=0)
    path: str | None = Field(default=None, max_length=500)
    hook_binding_id: str | None = Field(default=None, max_length=100)


@exported("observability")
class ObsScoreRecord(ZonicModel):
    score_id: str
    trace_id: str = Field(pattern=_HEX_32)
    observation_id: str | None = Field(default=None, pattern=_HEX_16)
    session_id: str | None = None
    name: str = Field(max_length=100)
    data_type: Literal["NUMERIC", "CATEGORICAL", "BOOLEAN", "CORRECTION", "TEXT"]
    value: float | None = None
    string_value: str | None = None
    source: Literal["API", "EVAL", "ANNOTATION"] = "API"
    comment: str | None = None
    author_user_id: str | None = Field(default=None, max_length=128)
    environment: str | None = Field(default=None, max_length=50)
    timestamp: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


@gateway_procedure(schema="observability", name="sp_obs_ingest", placeholders=3, idempotent=True)
class ObsIngestArgs(ZonicModel):
    """One batch: one transaction, idempotent, the one procedure the runtime retries."""

    traces: list[dict[str, Any]] = Field(default_factory=list)
    observations: list[dict[str, Any]] = Field(default_factory=list)
    scores: list[dict[str, Any]] = Field(default_factory=list)


# -- P3-11: what the trace, usage, feedback and maintenance APIs read and call ----------------------


class _ObsRow(ZonicModel):
    """Rows keep their snake_case column names; columns the API does not use are ignored."""

    model_config = ConfigDict(extra="ignore")


#: The owner of a trace's rows: the run's user (another user's trace is the same 404 as a missing one).
OWNER = "user_id"


@gateway_view(schema="observability", name="v_obs_trace", owner_column=OWNER)
class ObsTraceRow(_ObsRow):
    trace_id: str
    run_id: str | None = None
    name: str | None = None
    status: str | None = None
    session_id: str | None = None
    agentic_workflow_id: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    latency_ms: float | None = None
    observation_count: int | None = None
    error_count: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_cost: float | None = None
    tags: list[Any] | None = None
    user_id: str | None = None
    tenant_id: str | None = None


@gateway_view(schema="observability", name="v_obs_observation", owner_column=OWNER)
class ObsObservationRow(_ObsRow):
    observation_id: str
    trace_id: str
    parent_observation_id: str | None = None
    type: str
    name: str
    level: str | None = None
    status_message: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    latency_ms: float | None = None
    input: Any = None
    output: Any = None
    metadata: dict[str, Any] | None = None
    model: str | None = None
    usage_details: dict[str, Any] | None = None
    total_cost: float | None = None
    node_id: str | None = None
    node_type: str | None = None
    node_name: str | None = None
    visit: int | None = None
    path: str | None = None


@gateway_view(schema="observability", name="v_obs_score", owner_column=OWNER)
class ObsScoreRow(_ObsRow):
    score_id: str
    trace_id: str
    observation_id: str | None = None
    name: str
    data_type: str
    value: float | None = None
    string_value: str | None = None
    source: str | None = None
    comment: str | None = None
    timestamp: datetime | None = None


@gateway_view(schema="observability", name="v_obs_node_stats_daily")
class ObsNodeStatsRow(_ObsRow):
    day: str
    agentic_workflow_id: str | None = None
    node_type: str | None = None
    node_name: str | None = None
    executions: int = 0
    errors: int = 0
    error_rate: float | None = None
    avg_latency_ms: float | None = None
    p95_latency_ms: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_cost: float = 0


@gateway_view(schema="observability", name="v_obs_model_usage_daily")
class ObsModelUsageRow(_ObsRow):
    day: str
    model: str | None = None
    tenant_id: str | None = None
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_cost: float = 0


@gateway_procedure(schema="observability", name="sp_obs_score_upsert", placeholders=1, idempotent=True)
class ObsScoreUpsertArgs(ZonicModel):
    """A trace score (a message's feedback); the same ``scoreId`` replaces the earlier one."""

    score: dict[str, Any]


@gateway_procedure(schema="observability", name="sp_obs_partition_ensure", placeholders=1, idempotent=True)
class ObsPartitionEnsureArgs(ZonicModel):
    months_ahead: int = Field(ge=0)


@gateway_procedure(schema="observability", name="sp_obs_retention_purge", placeholders=2, idempotent=True)
class ObsRetentionPurgeArgs(ZonicModel):
    before: datetime


@gateway_procedure(schema="observability", name="sp_obs_refresh_rollups", placeholders=1, idempotent=True)
class ObsRefreshRollupsArgs(ZonicModel):
    """Refreshes the daily node and model rollups (no arguments)."""
