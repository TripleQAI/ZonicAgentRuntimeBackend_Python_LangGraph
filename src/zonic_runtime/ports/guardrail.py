"""The guardrail ports (P3-06, P3-10): a policy check of text, and a provisioned Bedrock guardrail.

``guardrail`` nodes and the ``guardrail`` hook use them. The policy model is
``engine/agents/policy_guard.PolicyGuard``; Bedrock Guardrails are
``integrations/guardrails/bedrock.BedrockGuardrails`` (``settings.guardrails.bedrockRegion``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

Source = Literal["INPUT", "OUTPUT"]


@dataclass(frozen=True)
class GuardrailDecision:
    allowed: bool
    reason: str = ""
    categories: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AppliedGuardrail:
    """A Bedrock guardrail's answer: whether it intervened, why, and the masked text when it did."""

    intervened: bool
    reason: str = ""
    categories: list[str] = field(default_factory=list)
    text: str | None = None


class GuardrailPort(Protocol):
    async def check(self, text: str, policy: str) -> GuardrailDecision: ...


class ManagedGuardrailPort(Protocol):
    async def apply(self, guardrail_id: str, version: str, source: Source, text: str) -> AppliedGuardrail: ...
