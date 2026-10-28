"""The template view of ``RunState`` (design 5.3, 7.1): what ``{{...}}`` may read.

The resolver reads plain namespace values; this module builds them from state so the
reducers' bookkeeping (provenance, order keys) never reaches a template.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from zonic_runtime.engine.state import CLEAR_THREAD, keyed_values
from zonic_runtime.workflow.templates.resolver import DEFAULT_THREAD, OUTPUT_FIELD, TemplateScope


def _content(entry: Mapping[str, Any]) -> Any:
    """A turn message's content as history has it: its text, or the value of a JSON part."""
    parts = [p for p in entry.get("contentParts") or [] if isinstance(p, Mapping)]
    texts = [p["text"] for p in parts if isinstance(p.get("text"), str)]
    if parts and len(texts) == len(parts):
        return "\n".join(texts)
    if len(parts) == 1 and "value" in parts[0]:
        return parts[0]["value"]
    return entry.get("textPreview", "")


def _messages(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """History then this turn's messages, every thread, with ``clear`` markers applied. Every
    message reads as ``{role, content, thread}`` (plus ``nodeId`` when a node wrote it)."""
    found = [
        {**message, "thread": thread}
        for thread, messages in (state.get("history") or {}).items()
        for message in messages
    ]
    for entry in state.get("turn_messages") or []:
        cleared = entry.get(CLEAR_THREAD)
        if cleared is not None:
            found = [m for m in found if m.get("thread", DEFAULT_THREAD) != cleared]
            continue
        message = {
            "role": entry.get("role"),
            "content": _content(entry),
            "thread": entry.get("thread", DEFAULT_THREAD),
        }
        if entry.get("nodeId"):
            message["nodeId"] = entry["nodeId"]
        found.append(message)
    return found


def _with_held(nodes: Mapping[str, Any], held: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Node records with the full values a dataset node keeps in the run for templates (its rows,
    its body) over the previews in its output (design 8.2, P3-04)."""
    found = dict(nodes)
    for node_id, values in held.items():
        record = found.get(node_id)
        if record is None:
            continue
        plain = (
            record.model_dump(mode="json", by_alias=True) if isinstance(record, BaseModel) else dict(record)
        )
        output = plain.get(OUTPUT_FIELD)
        found[node_id] = {
            **plain,
            OUTPUT_FIELD: {**(output if isinstance(output, Mapping) else {}), **values},
        }
    return found


def template_scope(
    state: Mapping[str, Any],
    *,
    run: Mapping[str, Any] | None = None,
    run_metadata: Mapping[str, Any] | None = None,
    interrupt_answer: Any = None,
    held: Mapping[str, Mapping[str, Any]] | None = None,
) -> TemplateScope:
    nodes = state.get("nodes") or {}
    return TemplateScope(
        system=keyed_values(state.get("system")),
        flow=keyed_values(state.get("flow")),
        nodes=_with_held(nodes, held) if held else dict(nodes),
        history=list((state.get("history") or {}).get(DEFAULT_THREAD, [])),
        messages=_messages(state),
        input=dict(state.get("input") or {}),
        run=dict(run or {}),
        run_metadata=dict(run_metadata or {}),
        interrupt_answer=interrupt_answer,
    )
