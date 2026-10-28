"""The template view of ``RunState`` (design 5.3, 7.1): what ``{{...}}`` may read.

The resolver reads plain namespace values; this module builds them from state so the
reducers' bookkeeping (provenance, order keys) never reaches a template.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from zonic_runtime.engine.state import CLEAR_THREAD, SEQ, keyed_values
from zonic_runtime.workflow.templates.resolver import DEFAULT_THREAD, TemplateScope


def _messages(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """History then this turn's messages, every thread, with ``clear`` markers applied."""
    found = [
        {**message, "thread": thread}
        for thread, messages in (state.get("history") or {}).items()
        for message in messages
    ]
    for entry in state.get("turn_messages") or []:
        cleared = entry.get(CLEAR_THREAD)
        if cleared is not None:
            found = [m for m in found if m.get("thread", DEFAULT_THREAD) != cleared]
        else:
            found.append({k: v for k, v in entry.items() if k != SEQ})
    return found


def template_scope(
    state: Mapping[str, Any],
    *,
    run: Mapping[str, Any] | None = None,
    run_metadata: Mapping[str, Any] | None = None,
    interrupt_answer: Any = None,
) -> TemplateScope:
    return TemplateScope(
        system=keyed_values(state.get("system")),
        flow=keyed_values(state.get("flow")),
        nodes=dict(state.get("nodes") or {}),
        history=list((state.get("history") or {}).get(DEFAULT_THREAD, [])),
        messages=_messages(state),
        input=dict(state.get("input") or {}),
        run=dict(run or {}),
        run_metadata=dict(run_metadata or {}),
        interrupt_answer=interrupt_answer,
    )
