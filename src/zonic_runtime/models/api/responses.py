"""Response models of the API (design Section 14; plan P2-03), ported from the Node.js runtime."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from zonic_runtime.models.base import ZonicModel
from zonic_runtime.models.engine import TimelineEntry, Usage
from zonic_runtime.models.gateway.runtime import RunMode, RunStatus, SessionStatus


class AgentCard(ZonicModel):
    agentic_workflow_id: str
    name: str = ""
    display_name: str | None = None
    description: str | None = None
    bpc: str | None = None
    updated_at: datetime | None = None


class AgentCardList(ZonicModel):
    items: list[AgentCard] = Field(default_factory=list)


class Session(ZonicModel):
    session_id: str
    agentic_workflow_id: str
    title: str = ""
    status: SessionStatus
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime


class SessionList(ZonicModel):
    items: list[Session] = Field(default_factory=list)
    #: Absent on the last page.
    next_cursor: str | None = None


class Message(ZonicModel):
    message_id: str
    session_id: str
    run_id: str | None = None
    node_id: str | None = None
    seq: int
    role: str
    kind: str
    visibility: str
    #: Content parts (design 14.2).
    content_parts: list[Any] = Field(default_factory=list)
    text_preview: str = ""
    created_at: datetime
    feedback_rating: int | None = None


class MessageList(ZonicModel):
    items: list[Message] = Field(default_factory=list)
    next_cursor: str | None = None


class DatasetPage(ZonicModel):
    """``GET /v1/datasets/{datasetId}``: one page of a dataset's rows, filtered and sorted."""

    dataset_id: str
    kind: str
    rows: list[dict[str, Any]] = Field(default_factory=list)
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    columns: list[str] = Field(default_factory=list)


class RunError(ZonicModel):
    """The user-safe error of a failed run: catalog code, message and retry flag."""

    code: str
    message: str
    retryable: bool = False


class Run(ZonicModel):
    run_id: str
    session_id: str
    agentic_workflow_id: str
    #: The workflow revision (canonical document hash) the run executed.
    revision: str = ""
    status: RunStatus
    mode: RunMode
    result: dict[str, Any] | None = None
    error: RunError | None = None
    usage: Usage | None = None
    queued_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    pending_interrupts: list[str] = Field(default_factory=list)


class RunResult(ZonicModel):
    """``POST /v1/runs`` in ``sync`` mode: the finished turn (``200``)."""

    run_id: str
    session_id: str
    status: RunStatus
    result: dict[str, Any] | None = None
    error: RunError | None = None
    usage: Usage | None = None


class RunInterrupt(ZonicModel):
    """A pending interrupt of a run (P4-02)."""

    interrupt_id: str
    run_id: str
    node_id: str
    #: The node type's form: ``approval`` or ``input``.
    kind: str
    #: ``text``: the question, with templates resolved.
    prompt: dict[str, Any] = Field(default_factory=dict)
    #: The JSON Schema the answer must match.
    answer_schema: dict[str, Any] = Field(default_factory=dict)
    ui_schema: dict[str, Any] = Field(default_factory=dict)
    requested_at: datetime
    expires_at: datetime | None = None


class RunInterruptList(ZonicModel):
    items: list[RunInterrupt] = Field(default_factory=list)


class RunLinks(ZonicModel):
    self_: str = Field(alias="self")
    events: str


class RunAccepted(ZonicModel):
    """``202``: the run goes on; poll ``links.self`` or follow ``links.events``."""

    run_id: str
    session_id: str
    status: RunStatus
    links: RunLinks


class NodeTypeInfo(ZonicModel):
    """One node type the runtime executes, with its ports and effective output schema."""

    type: str
    kind: str
    inputs: list[Any] = Field(default_factory=list)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    output_aliases: dict[str, str] = Field(default_factory=dict)


class NodeTypeList(ZonicModel):
    items: list[NodeTypeInfo] = Field(default_factory=list)


class NodeVisitList(ZonicModel):
    """``GET /v1/runs/{id}/nodes``: one entry per node visit, in completion order; no outputs."""

    items: list[TimelineEntry] = Field(default_factory=list)


class HealthStatus(ZonicModel):
    """``/healthz`` and ``/readyz``: ``ok``; ``degraded`` (ready, shedding new runs); or
    ``not_ready`` naming the failing dependency."""

    status: Literal["ok", "degraded", "not_ready"]
    release: str | None = None
    checks: dict[str, str] | None = None
