"""``Caller``: the authenticated user of a request (design 14.3, decision 0004).

Built from a verified Cognito access token by ``api/auth.py``; claim names come from
``settings.auth.claims``. The subject is the owner of every session, run and message the
caller creates, and every read is filtered by it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from zonic_runtime.workflow.loader.loader import WorkflowCaller


@dataclass(frozen=True)
class Caller:
    subject: str
    tenant: str | None = None
    email: str | None = None
    #: The ``groups`` claim (``cognito:groups``): roles such as ``runtime-support``.
    groups: frozenset[str] = frozenset()
    claims: Mapping[str, Any] = field(default_factory=dict)

    def workflows(self) -> WorkflowCaller:
        return WorkflowCaller(self.subject)

    def has_any(self, roles: list[str]) -> bool:
        return not self.groups.isdisjoint(roles)
