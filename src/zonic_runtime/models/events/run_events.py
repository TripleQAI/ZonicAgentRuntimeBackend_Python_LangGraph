"""Run events (design Section 10; plan P2-01): a live stream for clients, never a log and never state.

Every event shares the envelope ``{id, runId, seq, type, ts, nodeId?, path?, truncated?, data}``;
each type is a model, and the JSON Schemas are published under ``generated/schemas/events/``
(the client contract). The types and their data follow the Node.js runtime's contract, so one
client reads both runtimes.

The run stream builds envelopes as plain dictionaries from ``EventDraft`` (one per token
delta, so no model is built on the hot path); the models validate them in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import Field, RootModel

from zonic_runtime.models.base import ZonicModel
from zonic_runtime.models.content import ContentPart
from zonic_runtime.models.engine.records import Usage
from zonic_runtime.models.registry import exported

RunEventType = Literal[
    "run.started",
    "node.started",
    "node.finished",
    "token.delta",
    "reasoning.delta",
    "tool.started",
    "tool.finished",
    "swarm.member.started",
    "swarm.member.finished",
    "swarm.handoff",
    "loop.iteration",
    "content.part",
    "state.updated",
    "interrupt.requested",
    "warning",
    "run.waiting",
    "run.completed",
    "run.failed",
    "run.cancelled",
    "stream.gap",
    "heartbeat",
]


@dataclass(frozen=True, slots=True)
class EventDraft:
    """A run event before the run stream adds its envelope (``id``, ``runId``, ``seq``, ``ts``).
    ``data`` is already in wire form (camelCase keys, JSON values)."""

    type: RunEventType
    data: dict[str, Any] = field(default_factory=dict)
    node_id: str | None = None
    path: str | None = None


# --- data -----------------------------------------------------------------------------------


class RunStartedData(ZonicModel):
    session_id: str | None = None
    agentic_workflow_id: str
    revision: str
    title: str | None = None


class NodeStartedData(ZonicModel):
    node_type: str
    node_name: str
    visit: int = Field(ge=1)
    #: The node re-runs after an interrupt was answered.
    resumed: bool = False


class NodeFinishedData(ZonicModel):
    status: Literal["succeeded", "failed", "interrupted", "continued"]
    duration_ms: float = Field(ge=0)
    handles: list[str] = Field(default_factory=list)
    usage: Usage | None = None
    #: Types and sizes of the output, never its values (``run/events/summarizer.py``).
    output_summary: dict[str, Any] | None = None
    error_code: str | None = None
    #: The error policy that handled a failure (``continue`` or ``route``).
    policy: str | None = None


class TextDeltaData(ZonicModel):
    text: str
    #: The Swarm member that produced the text.
    member: str | None = None


class ToolStartedData(ZonicModel):
    tool_name: str
    tool_use_id: str
    input_summary: dict[str, Any] | None = None
    #: The Swarm member that called the tool.
    member: str | None = None


class ToolFinishedData(ZonicModel):
    tool_use_id: str
    status: Literal["success", "error"]
    duration_ms: float | None = Field(default=None, ge=0)
    member: str | None = None


class SwarmMemberData(ZonicModel):
    """A member step of a Swarm node; ``status``, ``durationMs`` and ``usage`` when it finished."""

    member: str
    status: Literal["completed", "failed", "cancelled"] | None = None
    duration_ms: float | None = Field(default=None, ge=0)
    usage: Usage | None = None


class SwarmHandoffData(ZonicModel):
    from_: str = Field(alias="from")
    to: list[str] = Field(default_factory=list)
    #: What the member handing off asked of the next one.
    message: str | None = None


class LoopIterationData(ZonicModel):
    loop_id: str
    iteration: int = Field(ge=1)


class ContentPartData(ZonicModel):
    """One content part a node produced (views), also kept in the turn's answer message."""

    type: str
    part: ContentPart


class StateUpdatedData(ZonicModel):
    #: Changed ``flow`` keys, names only.
    keys: list[str] = Field(default_factory=list)


class InterruptRequestedData(ZonicModel):
    interrupt_id: str
    kind: str
    prompt: str | None = None
    #: JSON Schema of the answer.
    schema_: dict[str, Any] = Field(default_factory=dict, alias="schema")
    expires_at: str | None = None


class WarningData(ZonicModel):
    code: str
    message: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class RunWaitingData(ZonicModel):
    interrupt_ids: list[str] = Field(default_factory=list)


class RunCompletedData(ZonicModel):
    result: dict[str, Any] = Field(default_factory=dict)
    usage: Usage | None = None


class RunFailedData(ZonicModel):
    code: str
    message: str
    retryable: bool = False


class RunCancelledData(ZonicModel):
    reason: str


class StreamGapData(ZonicModel):
    from_seq: int = Field(ge=1)
    #: The last missing event; absent when the run's buffer is gone and its length is unknown.
    to_seq: int | None = Field(default=None, ge=1)


class HeartbeatData(ZonicModel):
    pass


# --- envelopes ------------------------------------------------------------------------------


class _Envelope(ZonicModel):
    id: str
    run_id: str
    #: Strictly increasing per run from 1, continuing across a resume (DR-9); the SSE ``id``.
    seq: int = Field(ge=1)
    #: ISO 8601 UTC with milliseconds (``2026-10-27T01:59:11.256Z``).
    ts: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
    node_id: str | None = None
    path: str | None = None
    #: Set when the data was cut to ``settings.sse.maxEventBytes``.
    truncated: bool | None = None


@exported("events")
class RunStartedEvent(_Envelope):
    type: Literal["run.started"]
    data: RunStartedData


@exported("events")
class NodeStartedEvent(_Envelope):
    type: Literal["node.started"]
    data: NodeStartedData


@exported("events")
class NodeFinishedEvent(_Envelope):
    type: Literal["node.finished"]
    data: NodeFinishedData


@exported("events")
class TokenDeltaEvent(_Envelope):
    type: Literal["token.delta"]
    data: TextDeltaData


@exported("events")
class ReasoningDeltaEvent(_Envelope):
    type: Literal["reasoning.delta"]
    data: TextDeltaData


@exported("events")
class ToolStartedEvent(_Envelope):
    type: Literal["tool.started"]
    data: ToolStartedData


@exported("events")
class ToolFinishedEvent(_Envelope):
    type: Literal["tool.finished"]
    data: ToolFinishedData


@exported("events")
class SwarmMemberStartedEvent(_Envelope):
    type: Literal["swarm.member.started"]
    data: SwarmMemberData


@exported("events")
class SwarmMemberFinishedEvent(_Envelope):
    type: Literal["swarm.member.finished"]
    data: SwarmMemberData


@exported("events")
class SwarmHandoffEvent(_Envelope):
    type: Literal["swarm.handoff"]
    data: SwarmHandoffData


@exported("events")
class LoopIterationEvent(_Envelope):
    type: Literal["loop.iteration"]
    data: LoopIterationData


@exported("events")
class ContentPartEvent(_Envelope):
    type: Literal["content.part"]
    data: ContentPartData


@exported("events")
class StateUpdatedEvent(_Envelope):
    type: Literal["state.updated"]
    data: StateUpdatedData


@exported("events")
class InterruptRequestedEvent(_Envelope):
    type: Literal["interrupt.requested"]
    data: InterruptRequestedData


@exported("events")
class WarningEvent(_Envelope):
    type: Literal["warning"]
    data: WarningData


@exported("events")
class RunWaitingEvent(_Envelope):
    type: Literal["run.waiting"]
    data: RunWaitingData


@exported("events")
class RunCompletedEvent(_Envelope):
    type: Literal["run.completed"]
    data: RunCompletedData


@exported("events")
class RunFailedEvent(_Envelope):
    type: Literal["run.failed"]
    data: RunFailedData


@exported("events")
class RunCancelledEvent(_Envelope):
    type: Literal["run.cancelled"]
    data: RunCancelledData


@exported("events")
class StreamGapEvent(_Envelope):
    type: Literal["stream.gap"]
    data: StreamGapData


@exported("events")
class HeartbeatEvent(_Envelope):
    type: Literal["heartbeat"]
    data: HeartbeatData


AnyRunEvent = Annotated[
    RunStartedEvent
    | NodeStartedEvent
    | NodeFinishedEvent
    | TokenDeltaEvent
    | ReasoningDeltaEvent
    | ToolStartedEvent
    | ToolFinishedEvent
    | SwarmMemberStartedEvent
    | SwarmMemberFinishedEvent
    | SwarmHandoffEvent
    | LoopIterationEvent
    | ContentPartEvent
    | StateUpdatedEvent
    | InterruptRequestedEvent
    | WarningEvent
    | RunWaitingEvent
    | RunCompletedEvent
    | RunFailedEvent
    | RunCancelledEvent
    | StreamGapEvent
    | HeartbeatEvent,
    Field(discriminator="type"),
]


@exported("events")
class RunEvent(RootModel[AnyRunEvent]):
    """Any run event, by ``type``."""
