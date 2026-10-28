"""Request models of the API (design Section 14; plan P2-03).

Ported from the Node.js runtime's ``models/api/requests.ts`` (the Strands design's Section 12
contract), so one client works against both runtimes. Unknown fields are rejected with their
path (case A-09).
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from zonic_runtime.models.base import ZonicModel
from zonic_runtime.models.gateway.runtime import RunMode, SessionStatus


class CreateSessionRequest(ZonicModel):
    agentic_workflow_id: str = Field(min_length=1, max_length=100)
    title: str | None = Field(default=None, max_length=200)


class UpdateSessionRequest(ZonicModel):
    title: str | None = Field(default=None, max_length=200)
    status: SessionStatus | None = None


class RunInput(ZonicModel):
    #: The user's message; empty is ``INPUT_INVALID``, above ``settings.input.maxChars`` ``INPUT_TOO_LARGE``.
    text: str
    #: Attachment references (behind a feature flag in the UI).
    attachments: list[Any] | None = None


class RunCallback(ZonicModel):
    """Where an ``async`` run's completion summary is posted (P4-05): an allow-listed URL, signed
    with the HMAC key the secret reference names."""

    url: str = Field(min_length=1, max_length=2000)
    secret_ref: str = Field(pattern=r"^secret:[A-Za-z0-9_.-]+$")


class RunOptions(ZonicModel):
    callback: RunCallback | None = None


class StartRunRequest(ZonicModel):
    #: An existing session; without it, a session is created for ``agenticWorkflowId``.
    session_id: UUID | None = None
    agentic_workflow_id: str | None = Field(default=None, min_length=1, max_length=100)
    input: RunInput
    mode: RunMode
    options: RunOptions | None = None


class AnswerInterruptRequest(ZonicModel):
    """``POST /v1/runs/{runId}/interrupts/{interruptId}``: the answer, checked against the
    interrupt's ``answerSchema``, and the mode the resumed turn runs in."""

    answer: Any
    mode: RunMode = "stream"


class FeedbackRequest(ZonicModel):
    #: -1 or 1 for thumbs, 1 to 5 for stars.
    rating: int = Field(ge=-1, le=5)
    comment: str | None = Field(default=None, max_length=2000)


SessionListStatus = Literal["active", "archived", "all"]
MessageVisibility = Literal["chat", "internal"]
