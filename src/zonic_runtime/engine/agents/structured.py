"""The structured-output adapter (design 8.5; plan P2-02, case M-09).

A node's JSON Schema (an LLM node's ``outputSchema`` with ``responseFormat: json``, or the
platform's ``structuredOutput.schema``) needs a top-level ``title`` and ``description`` before a
provider accepts it as a tool or response format; the adapter adds them. The strategy is the
provider's native structured output when the catalog entry supports it (``ProviderStrategy``),
else a tool the model must call (``ToolStrategy``). Neither validates a JSON Schema dictionary
itself, so ``check`` does: an answer the schema rejects is ``STRUCTURED_OUTPUT_INVALID``.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from typing import Any

import jsonschema
from langchain.agents.structured_output import ProviderStrategy, ToolStrategy

from zonic_runtime.errors import ZonicError

#: Characters a provider accepts in a tool or schema name.
_NAME = re.compile(r"[^a-zA-Z0-9_-]+")
JSON_FORMAT = "json"
_JSON_FORMAT = JSON_FORMAT


def schema_name(name: str) -> str:
    return _NAME.sub("_", name).strip("_")[:64] or "answer"


def output_schema(schema: Mapping[str, Any], *, name: str, description: str | None = None) -> dict[str, Any]:
    adapted = copy.deepcopy(dict(schema))
    adapted.setdefault("type", "object")
    adapted["title"] = schema_name(str(adapted.get("title") or name))
    adapted.setdefault("description", description or f"The answer of {name}, as JSON.")
    return adapted


def node_schema(config: Any, name: str) -> dict[str, Any] | None:
    """The structured-output schema of an LLM or agent node, adapted; ``None`` for text."""
    structured = getattr(config, "structured_output", None)
    if isinstance(structured, Mapping) and isinstance(structured.get("schema"), Mapping):
        return output_schema(
            structured["schema"],
            name=str(structured.get("name") or name),
            description=structured.get("description"),
        )
    own = getattr(config, "output_schema", None)
    if getattr(config, "response_format", None) == _JSON_FORMAT and isinstance(own, Mapping) and own:
        return output_schema(own, name=name)
    return None


def strategy(schema: dict[str, Any], native: bool) -> ProviderStrategy[Any] | ToolStrategy[Any]:
    return ProviderStrategy(schema) if native else ToolStrategy(schema)


def check(value: Any, schema: Mapping[str, Any], node_id: str) -> Any:
    if value is None:
        raise ZonicError("STRUCTURED_OUTPUT_INVALID", node_id=node_id, reason="no structured answer")
    errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(value), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        raise ZonicError(
            "STRUCTURED_OUTPUT_INVALID",
            node_id=node_id,
            path="/".join(str(p) for p in first.path),
            reason=first.message[:300],
        )
    return value
