"""Rows and procedure arguments of ``agent_runtime`` (``db/runtime/004_views.sql``, ``005_procedures.sql``).

Ported from the Node.js runtime's ``runtime-rows.ts`` and ``runtime-procedures.ts``, which a
contract test there compares with the SQL. Every owner-scoped view carries ``owner_sub``, so
the repository adds the caller's owner filter to every read (design 14.3). Argument field order
is the positional order; JSON values travel as JSON text, INOUT parameters as trailing NULLs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from zonic_runtime.models.base import ZonicModel
from zonic_runtime.models.gateway_meta import gateway_procedure, gateway_view

SessionStatus = Literal["active", "archived"]
RunStatus = Literal["queued", "running", "waiting_input", "succeeded", "failed", "cancelled", "timed_out"]
RunMode = Literal["stream", "sync", "async"]
OWNER = "owner_sub"
#: Width of ``runtime_run.idempotency_key``.
IDEMPOTENCY_KEY_WIDTH = 200


class _Row(ZonicModel):
    """Rows keep their snake_case column names (no alias); the API maps them to its models."""


@gateway_view(schema="runtime", name="v_session_summary", owner_column=OWNER)
class SessionSummaryRow(_Row):
    session_id: UUID
    agentic_workflow_id: str
    title: str = ""
    status: SessionStatus
    tenant_id: str = ""
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime


@gateway_view(schema="runtime", name="v_run", owner_column=OWNER)
class RunRow(_Row):
    run_id: UUID
    session_id: UUID
    agentic_workflow_id: str
    workflow_revision: str = ""
    document_version: str = ""
    status: RunStatus
    mode: RunMode
    input: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None
    queued_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime
    attempt: int = Field(default=0, ge=0)
    tenant_id: str = ""
    #: The instance whose lease the run holds, and its internal address (P4-05).
    lease_owner: str | None = None
    lease_address: str | None = None
    has_checkpoint: bool = False
    callback: dict[str, Any] | None = None
    pending_interrupts: list[Any] = Field(default_factory=list)
    node_timeline: list[Any] = Field(default_factory=list)
    last_event_seq: int = Field(default=0, ge=0)


@gateway_view(schema="runtime", name="v_message", owner_column=OWNER)
class MessageRow(_Row):
    message_id: UUID
    session_id: UUID
    run_id: UUID | None = None
    node_id: str | None = None
    visit: int | None = None
    seq: int = Field(ge=1)
    role: str
    kind: str
    visibility: str = "chat"
    thread: str = "messages"
    content_parts: list[Any] = Field(default_factory=list)
    text_preview: str = ""
    created_at: datetime
    feedback_rating: int | None = None


@gateway_view(schema="runtime", name="v_dataset", owner_column=OWNER)
class DatasetRow(_Row):
    """A dataset's metadata (``chunk_no`` 0) or one body chunk of the ``database`` store (P3-05)."""

    dataset_id: UUID
    chunk_no: int = Field(ge=0)
    run_id: UUID
    session_id: UUID
    node_id: str = ""
    visit: int = 1
    store: str
    cache_key: str | None = None
    kind: str
    content_type: str = "application/json"
    row_count: int | None = None
    columns: list[Any] | None = None
    size_bytes: int = 0
    sha256: str = ""
    body: Any = None
    expires_at: datetime | None = None


@gateway_view(schema="runtime", name="v_session_state", owner_column=OWNER)
class SessionStateRow(_Row):
    session_id: UUID
    agentic_workflow_id: str
    flow: dict[str, Any] = Field(default_factory=dict)
    version: int = Field(default=0, ge=0)


@gateway_view(schema="runtime", name="v_checkpoint", owner_column=OWNER)
class CheckpointRow(_Row):
    run_id: UUID
    attempt: int = Field(default=0, ge=0)
    kind: Literal["pause", "drain", "async"]
    body: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


@gateway_view(schema="runtime", name="v_interrupt_pending", owner_column=OWNER)
class PendingInterruptRow(_Row):
    """A pending interrupt (P4-02). ``attempt`` is the pause's: rows of an earlier pause of the run
    that the run left pending (parallel interrupts answered one by one) are not current."""

    interrupt_id: UUID
    run_id: UUID
    session_id: UUID
    node_id: str
    kind: str
    prompt: dict[str, Any] = Field(default_factory=dict)
    answer_schema: dict[str, Any] = Field(default_factory=dict)
    ui_schema: dict[str, Any] = Field(default_factory=dict)
    status: str = "pending"
    requested_at: datetime
    expires_at: datetime | None = None
    attempt: int = Field(default=0, ge=0)


@gateway_view(schema="runtime", name="v_run")
class ClaimedRunRow(_Row):
    """``v_run`` read by the claimer for a run it claimed (P4-05): the system's read, never a
    user's; it carries the owner and the caller's snapshot the turn runs as."""

    run_id: UUID
    session_id: UUID
    agentic_workflow_id: str
    status: RunStatus
    mode: RunMode
    input: dict[str, Any] = Field(default_factory=dict)
    attempt: int = Field(default=0, ge=0)
    owner_sub: str
    tenant_id: str = ""
    has_checkpoint: bool = False
    callback: dict[str, Any] | None = None


@gateway_view(schema="runtime", name="v_interrupt_pending")
class ExpiringInterruptRow(_Row):
    """``v_interrupt_pending`` read by the expiry job across owners (P4-02): the system's read,
    never a user's; it carries the owner the job acts for."""

    interrupt_id: UUID
    run_id: UUID
    kind: str
    expires_at: datetime | None = None
    attempt: int = Field(default=0, ge=0)
    owner_sub: str


# --- procedures -----------------------------------------------------------------------------


@gateway_procedure(schema="runtime", name="sp_session_create", placeholders=1)
class SessionCreateArgs(ZonicModel):
    session: dict[str, Any]


@gateway_procedure(schema="runtime", name="sp_session_update")
class SessionUpdateArgs(ZonicModel):
    session_id: UUID
    owner_sub: str
    patch: dict[str, Any]


@gateway_procedure(schema="runtime", name="sp_run_start", placeholders=3)
class RunStartArgs(ZonicModel):
    run: dict[str, Any]
    user_message: dict[str, Any] | None = None


@gateway_procedure(schema="runtime", name="sp_run_transition")
class RunTransitionArgs(ZonicModel):
    run_id: UUID
    to: RunStatus
    patch: dict[str, Any] = Field(default_factory=dict)


@gateway_procedure(schema="runtime", name="sp_run_finish", idempotent=True)
class RunFinishArgs(ZonicModel):
    run_id: UUID
    attempt: int = Field(ge=0)
    save: dict[str, Any]


@gateway_procedure(schema="runtime", name="sp_dataset_save", idempotent=True)
class DatasetSaveArgs(ZonicModel):
    """The body chunks of the turn's ``database``-store datasets (P3-05): the first item of the
    transactional turn-end batch, before ``sp_run_finish`` saves their metadata."""

    run_id: UUID
    attempt: int = Field(ge=0)
    chunks: list[dict[str, Any]]


@gateway_procedure(schema="runtime", name="sp_retention_purge", idempotent=True)
class RetentionPurgeArgs(ZonicModel):
    """Runtime retention (P3-11): expired checkpoints, interrupts and datasets; idle sessions archived."""

    before: datetime


@gateway_procedure(schema="runtime", name="sp_feedback_save")
class FeedbackSaveArgs(ZonicModel):
    feedback: dict[str, Any]


@gateway_procedure(schema="runtime", name="sp_interrupt_create", placeholders=1, idempotent=True)
class InterruptCreateArgs(ZonicModel):
    """The pause save (P4-02): ``{runId, interrupts: [...]}``, the checkpoint row ``{body,
    expiresAt}`` and the turn's messages so far, in one transaction; idempotent per interrupt."""

    interrupt: dict[str, Any]
    checkpoint: dict[str, Any]
    messages: list[dict[str, Any]]
    attempt: int = Field(ge=0)


@gateway_procedure(schema="runtime", name="sp_interrupt_answer")
class InterruptAnswerArgs(ZonicModel):
    interrupt_id: UUID
    owner_sub: str
    answer: Any


@gateway_procedure(schema="runtime", name="sp_run_claim", placeholders=1)
class RunClaimArgs(ZonicModel):
    """Up to ``limit`` queued runs (round-robin across tenants) and running runs whose lease expired,
    leased to ``instance`` ``{instanceId, address}``; each claim increments the run's attempt."""

    instance: dict[str, Any]
    limit: int = Field(gt=0)
    lease_seconds: int = Field(gt=0)


@gateway_procedure(schema="runtime", name="sp_run_heartbeat", placeholders=1, idempotent=True)
class RunHeartbeatArgs(ZonicModel):
    """Renews the leases the instance still holds; the others come back as lost."""

    instance_id: str
    run_ids: list[str]
    lease_seconds: int = Field(gt=0)


@gateway_procedure(schema="runtime", name="sp_run_release", idempotent=True)
class RunReleaseArgs(ZonicModel):
    """On drain: the instance's ``async`` runs back to the queue; its others failed with ``SHUTDOWN``."""

    instance_id: str
    run_ids: list[str]


@gateway_procedure(schema="runtime", name="sp_checkpoint_save", idempotent=True)
class CheckpointSaveArgs(ZonicModel):
    """A running run's checkpoint (P4-05): a drained ``async`` run's, or write-behind; latest wins."""

    run_id: UUID
    attempt: int = Field(ge=0)
    body: dict[str, Any]


#: INOUT outputs of the procedures above.
SESSION_ID_OUT = "o_session_id"
RUN_ID_OUT, MESSAGE_ID_OUT = "o_run_id", "o_message_id"
INTERRUPT_ID_OUT = "o_interrupt_id"
RUNS_OUT, LOST_OUT = "o_runs", "o_lost"
