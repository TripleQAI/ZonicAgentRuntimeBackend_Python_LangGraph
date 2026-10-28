"""Interrupt row IDs (plan P4-02; decision 0007).

A LangGraph interrupt keeps its ID while it is pending, also when a run with two pending
interrupts resumes with one answer and pauses again on the other (SP-8). ``sp_interrupt_create``
is idempotent per interrupt ID, so the second pause would be a no-op under the same ID: each pause
writes its interrupts under IDs of its own attempt, and the checkpoint maps them back to
LangGraph's. The API and the events use these row IDs.
"""

from __future__ import annotations

import uuid

_PREFIX = "zonic-interrupt"


def row_id(run_id: str, attempt: int, interrupt_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{_PREFIX}:{run_id}:{attempt}:{interrupt_id}"))
