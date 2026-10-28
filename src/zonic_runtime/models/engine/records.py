"""The node contract and the values of ``RunState`` (design 7.1, 8.1, 9.3, 9.4; plan P1-02, DR-1).

Nodes never build state dictionaries by hand (design rule 15): they build these models
and the template turns them into LangGraph updates (P1-08). All are JSON-compatible, so
the checkpoint serializer never needs a pickle fallback.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from zonic_runtime.models.base import ZonicModel
from zonic_runtime.models.nodes.base import ConditionGroup

NodeStatus = Literal["succeeded", "failed", "interrupted", "continued"]
MessageRole = Literal["user", "assistant", "system", "tool"]  # principle: allow rule-1-node-type message-role
MessageKind = Literal["user_input", "answer", "agent_turn", "tool_call", "tool_result", "handoff"]
InterruptKind = Literal["input", "approval", "tool_approval"]  # principle: allow rule-1-node-type db-enum
UpdateOp = Literal["set", "append", "extend", "merge", "increment", "remove", "clear"]

#: ``NodeOutcome.activate`` value meaning every outgoing edge.
ALL_HANDLES = "*"


class Usage(ZonicModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    model_calls: int = 0
    cost: float | None = None

    def __add__(self, other: Usage) -> Usage:
        cost = None if self.cost is None and other.cost is None else (self.cost or 0) + (other.cost or 0)
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            model_calls=self.model_calls + other.model_calls,
            cost=cost,
        )


class DatasetRef(ZonicModel):
    """Full data of a database, HTTP or tool result, kept outside state (design 9.3). The wire
    shape is the Node.js runtime's (content parts carry it); ``node_id``, ``visit`` and
    ``preview`` are this runtime's provenance and inline rows."""

    store: Literal["cache", "database"]
    #: The cache key (``cache`` store) or the dataset ID (``database`` store).
    key: str
    #: The public ID: ``GET /v1/datasets/{datasetId}`` (P3-05); absent until registered.
    dataset_id: str | None = None
    kind: Literal["rows", "http_response", "tool_result"] = "rows"
    content_type: str | None = None
    row_count: int | None = Field(default=None, ge=0)
    columns: list[str] = Field(default_factory=list)
    size_bytes: int | None = Field(default=None, ge=0)
    sha256: str | None = None
    expires_at: datetime | None = None
    node_id: str | None = None
    visit: int | None = Field(default=None, ge=1)
    preview: list[dict[str, Any]] = Field(default_factory=list)


class ThreadMessage(ZonicModel):
    """One chat message of this turn; saved to ``runtime_message`` at turn end."""

    role: MessageRole
    kind: MessageKind
    thread: str = "messages"
    visibility: Literal["chat", "internal"] = "chat"
    node_id: str | None = None
    visit: int | None = None
    content_parts: list[dict[str, Any]] = Field(default_factory=list)
    text_preview: str = ""


class StateUpdate(ZonicModel):
    """One canonical state operation (the normalizer maps both dialects' spellings to it).

    ``path`` may itself be a template (platform ``fieldName``); ``role`` is the message role
    of a ``thread.*`` append; ``when`` skips the update when its conditions fail.
    """

    path: str
    op: UpdateOp = "set"
    value: Any = None
    role: str | None = None
    when: ConditionGroup | None = None


class FlowPatch(ZonicModel):
    """Writes to ``flow`` (or ``system``) with their provenance (DR-1): superstep and writer node ID.

    ``set`` holds new top-level values; ``remove`` lists top-level keys an operation deleted.
    """

    step: int = Field(ge=-1)  # -1: the turn loader, before the first superstep
    writer: str
    set: dict[str, Any] = Field(default_factory=dict)
    remove: list[str] = Field(default_factory=list)


class TimelineEntry(ZonicModel):
    """Compact per-visit record, saved at turn end."""

    node_id: str
    node_type: str
    visit: int = Field(ge=1)
    status: NodeStatus
    started_at: datetime
    duration_ms: float = Field(ge=0)
    handles: list[str] = Field(default_factory=list)
    error_code: str | None = None
    #: A sub-flow's visit: the sub-flow node names leading to it (``agent/lookup``); none at the root.
    path: str | None = None


class NodeRecord(ZonicModel):
    """A node's result in ``RunState.nodes``, by node ID; ``{{nodes.X.output...}}`` reads it."""

    node_id: str
    node_type: str
    name: str | None = None
    status: NodeStatus
    visit: int = Field(ge=1)
    output: dict[str, Any] = Field(default_factory=dict)
    usage: Usage | None = None
    error: dict[str, Any] | None = None
    #: The visit's content parts (views); the turn's answer message carries them (P3-08).
    parts: list[dict[str, Any]] = Field(default_factory=list)


class NodeOutcome(ZonicModel):
    """What a node kind returns: its output, the handles it activates, and side records."""

    output: dict[str, Any] = Field(default_factory=dict)
    activate: list[str] = Field(default_factory=list)
    status: NodeStatus = "succeeded"
    usage: Usage | None = None
    messages: list[ThreadMessage] = Field(default_factory=list)
    datasets: list[DatasetRef] = Field(default_factory=list)
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    #: Content parts for clients (``models/content``), plain; the view kind also emits each one.
    parts: list[dict[str, Any]] = Field(default_factory=list)
    #: Visits of a child workflow (sub-flows), with their ``path``; before the node's own visit.
    timeline: list[TimelineEntry] = Field(default_factory=list)
    #: The run result, set by the Output kind (``first_result`` keeps the first).
    result: dict[str, Any] | None = None
    #: State operations of the kind itself (the Variable node's ``updates``); the template
    #: applies them before the node's ``variableUpdates`` (design 8.1 step 9).
    updates: list[StateUpdate] = Field(default_factory=list)
    #: ``output`` is already output data (a hook's ``skip`` or ``recover``), not the kind's raw result.
    mapped: bool = False

    @property
    def activates_all(self) -> bool:
        return ALL_HANDLES in self.activate


class InterruptRequest(ZonicModel):
    """The JSON an interrupt kind passes to ``interrupt()`` (design 9.4)."""

    kind: InterruptKind
    node_id: str
    prompt: dict[str, Any] | str
    answer_schema: dict[str, Any] | None = None
    ui_schema: dict[str, Any] | None = None
    expires_at: datetime | None = None


class StatePatch(ZonicModel):
    """Everything one node visit writes to ``RunState``; P1-08 turns it into a LangGraph update."""

    flow: FlowPatch | None = None
    system: dict[str, Any] = Field(default_factory=dict)
    node: NodeRecord | None = None
    messages: list[ThreadMessage] = Field(default_factory=list)
    timeline: list[TimelineEntry] = Field(default_factory=list)
    datasets: list[DatasetRef] = Field(default_factory=list)
    result: dict[str, Any] | None = None
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    usage: Usage | None = None
