"""The policy model (plan P3-06, P3-10), after the Node.js runtime's ``policy-guard.ts``.

``settings.guardrails.model`` (else the catalog default) with ``settings.guardrails.prompt``
(``{policy}`` replaced), asked for a structured decision on the text: ``allowed``, ``reason`` and
the ``categories`` it breaks. ``guardrail`` nodes ask it inside their visit, so the call streams
and traces like any LLM node's; the ``guardrail`` hook asks it through ``PolicyGuard``.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from zonic_runtime.engine.agents.structured import check
from zonic_runtime.errors import ZonicError
from zonic_runtime.models.dialects.refs import ModelRef
from zonic_runtime.models.resources.settings import GuardrailSettings
from zonic_runtime.ports.guardrail import GuardrailDecision

#: Native structured output by JSON Schema (OpenAI and Azure OpenAI).
NATIVE_METHOD = "json_schema"
#: The policy model's decision.
DECISION: Mapping[str, Any] = MappingProxyType(
    {
        "title": "GuardrailDecision",
        "description": "Whether the content complies with the policy.",
        "type": "object",
        "properties": {
            "allowed": {"type": "boolean", "description": "true when the content complies with the policy"},
            "reason": {"type": "string", "description": "why, in one sentence"},
            "categories": {
                "type": "array",
                "items": {"type": "string"},
                "description": "the policy rules the content breaks",
            },
        },
        "required": ["allowed", "reason"],
    }
)


def guardrail_prompt(settings: GuardrailSettings, policy: str) -> str:
    return settings.prompt.replace("{policy}", policy)


def policy_ref(settings: GuardrailSettings) -> ModelRef | None:
    return ModelRef(model_id=settings.model) if settings.model else None


def decision_messages(settings: GuardrailSettings, policy: str, text: str) -> list[BaseMessage]:
    return [SystemMessage(guardrail_prompt(settings, policy)), HumanMessage(text)]


def decision_of(value: Any, node_id: str = "") -> GuardrailDecision:
    found = check(value, dict(DECISION), node_id)
    categories = found.get("categories")
    return GuardrailDecision(
        allowed=found.get("allowed") is True,
        reason=str(found.get("reason") or ""),
        categories=[str(c) for c in categories] if isinstance(categories, list) else [],
    )


async def decide(
    model: BaseChatModel, native: bool, messages: list[BaseMessage], config: Any = None, node_id: str = ""
) -> tuple[GuardrailDecision, BaseMessage]:
    """One structured model call; the decision and the raw answer (for its usage)."""
    options = {"method": NATIVE_METHOD} if native else {}
    found = await model.with_structured_output(dict(DECISION), include_raw=True, **options).ainvoke(
        messages, config=config
    )
    if not isinstance(found, Mapping):
        raise ZonicError("STRUCTURED_OUTPUT_INVALID", node_id=node_id, reason="guardrail decision")
    parsed = None if found.get("parsing_error") else found.get("parsed")
    return decision_of(parsed, node_id), found["raw"]


class PolicyGuard:
    """``GuardrailPort`` over the model catalog (the ``guardrail`` hook's ``policy_model``)."""

    def __init__(self, settings: GuardrailSettings, models: Any) -> None:
        self._settings, self._models = settings, models
        self._model: BaseChatModel | None = None

    async def check(self, text: str, policy: str) -> GuardrailDecision:
        ref = policy_ref(self._settings)
        if self._model is None:
            self._model = self._models.create(ref, stream=False)
        native = self._models.entry(ref).supports_native_structured_output
        decision, _ = await decide(self._model, native, decision_messages(self._settings, policy, text))
        return decision
