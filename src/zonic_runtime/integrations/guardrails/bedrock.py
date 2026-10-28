"""Bedrock Guardrails (plan P3-06): a provisioned guardrail applied to text (``ApplyGuardrail`` on
the ``bedrock-runtime`` client of ``settings.guardrails.bedrockRegion``), after the Node.js
runtime's adapter. The client call is blocking, so it runs on the loop's default executor.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any

from zonic_runtime.errors import ZonicError
from zonic_runtime.ports.guardrail import AppliedGuardrail, Source

_INTERVENED = "GUARDRAIL_INTERVENED"


def _categories(assessments: Any) -> list[str]:
    """The policies that acted: topics, content filters, words and sensitive information."""
    found: dict[str, None] = {}
    for item in assessments if isinstance(assessments, list) else []:
        if not isinstance(item, Mapping):
            continue
        for group, inner, key in (
            ("topicPolicy", "topics", "name"),
            ("contentPolicy", "filters", "type"),
            ("wordPolicy", "customWords", "match"),
            ("sensitiveInformationPolicy", "piiEntities", "type"),
        ):
            for entry in (item.get(group) or {}).get(inner) or []:
                if isinstance(entry, Mapping) and entry.get("action") not in (None, "NONE"):
                    found[str(entry.get(key))] = None
    return list(found)


class BedrockGuardrails:
    def __init__(self, region: str, clients: Callable[[str | None], Any]) -> None:
        self._region, self._clients = region, clients

    async def apply(self, guardrail_id: str, version: str, source: Source, text: str) -> AppliedGuardrail:
        client = self._clients(self._region)

        def call() -> Any:
            return client.apply_guardrail(
                guardrailIdentifier=guardrail_id,
                guardrailVersion=version,
                source=source,
                content=[{"text": {"text": text}}],
            )

        try:
            answer = await asyncio.get_running_loop().run_in_executor(None, call)
        except Exception as exc:  # botocore's errors: the guardrail did not answer
            raise ZonicError(
                "MODEL_UNAVAILABLE", provider="bedrock-guardrails", reason=str(exc)[:300]
            ) from exc
        intervened = answer.get("action") == _INTERVENED
        outputs = [o.get("text") for o in answer.get("outputs") or [] if isinstance(o, Mapping)]
        categories = _categories(answer.get("assessments"))
        return AppliedGuardrail(
            intervened=intervened,
            reason=", ".join(categories) or ("the guardrail intervened" if intervened else ""),
            categories=categories,
            text="\n".join(t for t in outputs if isinstance(t, str)) if intervened and outputs else None,
        )
