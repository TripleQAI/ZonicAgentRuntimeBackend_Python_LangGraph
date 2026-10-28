"""What a Swarm member's step starts with (design 8.6, "Context"; plan P4-01; case W-07).

A member's own part comes first: its system prompt and the non-system messages of its
configuration (an LLM member's ``messages``), resolved by the kind at the Swarm's visit (the
``brief``). Then:

* the first step: the task;
* ``shared`` (the default, the builder's documented behaviour): one user message with the
  original task, the handoffs so far, the other members and the last handoff message (the lines
  of ``settings.swarm.sharedContext``), then every handoff pair of the visit;
* ``handoff``: the last handoff message, then that handoff's pair only.

The handoff pairs are the Swarm's ``messages``: each a model message with one transfer call and
the ``ToolMessage`` acknowledging it, so the history is valid for every provider.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from zonic_runtime.engine.agents.messages import ChatText, to_langchain
from zonic_runtime.engine.agents.swarm.handoff_tools import SwarmMemberRef
from zonic_runtime.models.resources.settings import SwarmSettings

_HANDOFF = "handoff"


def fill(line: str, **values: str) -> str:
    for key, value in values.items():
        line = line.replace(f"{{{key}}}", value)
    return line


def brief(system: Sequence[str], messages: Sequence[ChatText]) -> dict[str, Any]:
    """A member's own part of its input, as the Swarm state keeps it."""
    return {
        "system": "\n\n".join(s for s in system if s.strip()),
        "messages": [{"role": m.role, "content": m.content} for m in messages],
    }


def member_messages(
    own: Mapping[str, Any],
    state: Mapping[str, Any],
    member: SwarmMemberRef,
    others: Sequence[SwarmMemberRef],
    settings: SwarmSettings,
) -> list[BaseMessage]:
    found: list[BaseMessage] = [SystemMessage(own["system"])] if own.get("system") else []
    found += to_langchain([ChatText(m["role"], m["content"]) for m in own.get("messages") or []])
    task = str(state.get("task") or "")
    handoffs = list(state.get("handoffs") or [])
    if not handoffs:
        return [*found, HumanMessage(task)]
    last = handoffs[-1]
    message = str(last.get("message") or "")
    pairs = list(state.get("messages") or [])
    if settings.context_mode == _HANDOFF:
        return [*found, HumanMessage(message or task), *pairs[-2:]]
    lines = settings.shared_context
    available = "; ".join(f"{m.name} ({m.description})" if m.description else m.name for m in others)
    preamble = [
        fill(lines.task, task=task),
        fill(lines.history, history=", ".join(f"{h.get('from')} -> {h.get('to')}" for h in handoffs)),
        fill(lines.members, members=available),
    ]
    if message:
        preamble.append(fill(lines.message, message=message))
    return [*found, HumanMessage("\n".join(preamble)), *pairs]
