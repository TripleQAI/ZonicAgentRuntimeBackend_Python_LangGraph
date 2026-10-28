"""Approval requests: an Approval node's, an ``approval`` hook's, a tool call's (design 9.4; plan
P4-02, P4-03).

All three are answered with ``ApprovalAnswer``, so the API adds ``decidedBy`` to each and the
expiry treats each alike. ``approval_request`` builds the ``interrupt()`` value of an ``approval``
hook: before a node (the template's step 2) or before a tool call (``HookMiddleware``).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from zonic_runtime.models.base import ZonicModel
from zonic_runtime.models.engine import InterruptRequest
from zonic_runtime.models.hooks.result import ApprovalHookConfig

APPROVAL_KIND = "approval"  # principle: allow rule-1-node-type interrupt-kind-of-the-db-enum
#: The prompt member naming a tool call's tool (tool approvals, ``approval`` hooks on a tool call).
TOOL_KEY = "tool"  # principle: allow rule-1-node-type approval-prompt-key
_TEXT = "text"


class ApprovalAnswer(ZonicModel):
    """The decision on an Approval. The runtime sets decidedBy (who answered) and expired."""

    approved: bool = Field(description="true approves, false rejects")
    comment: str | None = Field(default=None, max_length=2000)
    decided_by: str | None = Field(default=None, json_schema_extra={"readOnly": True})
    expired: bool = Field(default=False, json_schema_extra={"readOnly": True})


def answer_value(answer: Any) -> Any:
    """What ``interrupt()`` returned, as JSON: a validated model's fields by their wire names."""
    return answer.model_dump(mode="json", by_alias=True) if isinstance(answer, BaseModel) else answer


def approval_request(
    binding_id: str,
    config: ApprovalHookConfig,
    node_id: str,
    prompt: Any,
    now: datetime,
    default_hours: float,
    extra: Mapping[str, Any] | None = None,
) -> InterruptRequest:
    """An ``approval`` hook's request: its prompt, the binding and, for a tool call, the call."""
    text = prompt if isinstance(prompt, str) else "" if prompt is None else str(prompt)
    return InterruptRequest(
        kind=APPROVAL_KIND,
        node_id=node_id,
        prompt={_TEXT: text, "binding": binding_id, **(extra or {})},
        answer_schema=ApprovalAnswer.model_json_schema(by_alias=True),
        ui_schema={},
        expires_at=now + timedelta(hours=config.timeout_hours or default_hours),
    )
