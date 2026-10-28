"""Interrupt answers the API publishes (design 3.6: the JSON Schemas of interrupt answers).

``ApprovalAnswer`` answers an Approval node, an ``approval`` hook and a tool approval's
``ApprovalAnswer``-shaped prompts alike; ``InputAnswer`` is a Human Input node's answer, which each
interrupt's own ``answerSchema`` narrows.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from zonic_runtime.models.base import ZonicModel
from zonic_runtime.models.registry import exported


@exported("answers")
class ApprovalAnswer(ZonicModel):
    """The decision on an Approval. The runtime sets decidedBy (who answered) and expired."""

    approved: bool = Field(description="true approves, false rejects")
    comment: str | None = Field(default=None, max_length=2000)
    decided_by: str | None = Field(default=None, json_schema_extra={"readOnly": True})
    expired: bool = Field(default=False, json_schema_extra={"readOnly": True})


@exported("answers")
class InputAnswer(ZonicModel):
    """The published shape of a Human Input node's answer. Each interrupt's ``answerSchema`` narrows
    ``value`` to the node's input type (``InterruptKind.answer_model``)."""

    value: Any = Field(description="The answer, of the type the interrupt's answerSchema names")
