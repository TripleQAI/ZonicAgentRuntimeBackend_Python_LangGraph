"""The run result after the graph finishes (design 8.1 "Run result"; plan P1-10, case E-18).

The Output kind writes ``result``; the first Output wins (``first_result``) and every other
one is reported. When no Output ran, ``settings.result.fallback`` applies in order (the
last agent text, then the last LLM text, by timeline order), else ``NO_OUTPUT``. A sub-flow
node's result is an answer too (its child's answer: its text, else the child's whole result, as
the child's own chat would show it), and a child's own visits in the timeline (those with a
``path``) are its child's, not this workflow's (P3-09).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from zonic_runtime.errors import ZonicError
from zonic_runtime.models.blueprint import Blueprint
from zonic_runtime.workflow.templates.resolver import OUTPUT_FIELD

#: ``last_<kind>_text``: the last text of a node of that kind.
_FALLBACK = re.compile(r"^last_(?P<kind>[a-z_]+)_text$")
_SUBFLOW = "subflow"
#: ``RunResult.source`` when an Output node set the result.
FROM_OUTPUT_NODE = "outputNode"


@dataclass(frozen=True)
class RunResult:
    value: dict[str, Any]
    source: str  # FROM_OUTPUT_NODE, or the fallback used
    warnings: list[dict[str, Any]] = field(default_factory=list)


def answer_text(result: Mapping[str, Any]) -> str | None:
    """A result's answer as text: its ``text``, else its only string field; ``None`` when the
    result names no single answer (the chat then shows it as JSON)."""
    if isinstance(result.get("text"), str):
        return str(result["text"])
    strings = [v for v in result.values() if isinstance(v, str)]
    return strings[0] if len(strings) == 1 else None


def run_result(state: Mapping[str, Any], blueprint: Blueprint, fallback: Sequence[str]) -> RunResult:
    found = state.get("result")
    if found is not None:
        warnings = [
            {"code": "OUTPUT_IGNORED", "nodeId": writer, "kept": found["writer"]}
            for writer in found.get("ignored", [])
        ]
        return RunResult(value=dict(found["value"]), source=FROM_OUTPUT_NODE, warnings=warnings)
    order = [
        entry["nodeId"]
        for entry in state.get("timeline") or []
        if entry.get("status") == "succeeded" and not entry.get("path")
    ]
    records = state.get("nodes") or {}
    for name in fallback:
        match = _FALLBACK.match(name)
        kind = match.group("kind") if match else None
        for node_id in reversed(order):
            spec = blueprint.nodes.get(node_id)
            output = (records.get(node_id) or {}).get(OUTPUT_FIELD) or {}
            text = output.get("text")
            if spec is not None and spec.kind in (kind, _SUBFLOW) and isinstance(text, str) and text:
                return RunResult(value={"text": text}, source=name)
            if spec is not None and spec.kind == _SUBFLOW and output:
                return RunResult(value=dict(output), source=name)
    raise ZonicError("NO_OUTPUT")
