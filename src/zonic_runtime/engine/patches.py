"""``StatePatch`` to a LangGraph update (design 7.1, 8.1; plan P1-08, DR-1, DR-4).

Nodes never build state dictionaries (rule 15): the template builds a ``StatePatch`` and
``to_update`` turns it into the update the reducers of ``engine/state.py`` expect, stamping
every write with its superstep and writer. ``system`` writes outside
``settings.templates.writableSystemKeys`` fail here with ``STATE_READ_ONLY``, before the
update leaves the node (DR-4): a reducer cannot know which keys are writable.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Any, Literal, cast, get_args

from pydantic import BaseModel

from zonic_runtime.engine.state import CLEAR_THREAD, RUN_VISITS, SEQ
from zonic_runtime.errors import ZonicError
from zonic_runtime.models.engine import FlowPatch, StatePatch, ThreadMessage
from zonic_runtime.models.engine.records import MessageKind, MessageRole
from zonic_runtime.workflow.templates.resolver import DEFAULT_THREAD
from zonic_runtime.workflow.templates.updates import UpdateResult

_PREVIEW_CHARS = 200
_ROLES = get_args(MessageRole)


def _dump(value: Any) -> Any:
    return (
        value.model_dump(mode="json", by_alias=True, exclude_none=True)
        if isinstance(value, BaseModel)
        else value
    )


def _entries(items: Sequence[Any], step: int, writer: str, start: int = 0) -> list[dict[str, Any]]:
    return [{**_dump(item), SEQ: [step, writer, start + i]} for i, item in enumerate(items)]


def to_update(
    patch: StatePatch,
    *,
    step: int,
    writer: str,
    writable_system_keys: Collection[str],
    cleared_threads: Sequence[str] = (),
) -> dict[str, Any]:
    """The LangGraph update of one node visit. ``writer`` is the builder node ID."""
    update: dict[str, Any] = {}
    if patch.flow is not None and (patch.flow.set or patch.flow.remove):
        update["flow"] = {"step": step, "writer": writer, "set": patch.flow.set, "remove": patch.flow.remove}
    if patch.system:
        forbidden = sorted(k for k in patch.system if k not in writable_system_keys)
        if forbidden:
            raise ZonicError("STATE_READ_ONLY", path=f"system.{forbidden[0]}", node_id=writer)
        update["system"] = {"step": step, "writer": writer, "set": dict(patch.system), "remove": []}
    if patch.node is not None:
        update["nodes"] = {patch.node.node_id: _dump(patch.node)}
        update["visits"] = {patch.node.node_id: 1, RUN_VISITS: 1}
    clears = [{CLEAR_THREAD: t, SEQ: [step, writer, i]} for i, t in enumerate(cleared_threads)]
    if patch.messages or clears:
        update["turn_messages"] = [*clears, *_entries(patch.messages, step, writer, len(clears))]
    for name, items in (
        ("timeline", patch.timeline),
        ("datasets", patch.datasets),
        ("warnings", patch.warnings),
        ("errors", patch.errors),
    ):
        if items:
            update[name] = _entries(items, step, writer)
    if patch.result is not None:
        update["result"] = {"value": patch.result, "step": step, "writer": writer, "ignored": []}
    if patch.usage is not None:
        update["usage"] = _dump(patch.usage)
    return update


def _part(content: Any) -> dict[str, Any]:
    return (
        {"type": "markdown", "text": content}
        if isinstance(content, str)
        else {"type": "json", "value": content}
    )


def thread_message(
    role: MessageRole,
    kind: MessageKind,
    content: Any,
    *,
    thread: str = DEFAULT_THREAD,
    visibility: Literal["chat", "internal"] | None = None,
    node_id: str | None = None,
    visit: int | None = None,
) -> ThreadMessage:
    """One message of the turn; the chat thread is visible unless ``visibility`` says otherwise."""
    return ThreadMessage(
        role=role,
        kind=kind,
        thread=thread,
        visibility=visibility or ("chat" if thread == DEFAULT_THREAD else "internal"),
        node_id=node_id,
        visit=visit,
        content_parts=[_part(content)],
        text_preview=(content if isinstance(content, str) else str(content))[:_PREVIEW_CHARS],
    )


def update_messages(result: UpdateResult, node_id: str | None, visit: int | None) -> list[ThreadMessage]:
    """The messages an ``UpdateWriter`` result appends, as ``ThreadMessage`` models."""
    messages = []
    for written in result.messages:
        role = cast(MessageRole, written["role"] if written["role"] in _ROLES else "assistant")
        kind: MessageKind = "user_input" if role == "user" else "agent_turn"
        thread = written.get("thread", DEFAULT_THREAD)
        messages.append(
            thread_message(role, kind, written.get("content"), thread=thread, node_id=node_id, visit=visit)
        )
    return messages


def flow_patch(result: UpdateResult, step: int, writer: str) -> FlowPatch | None:
    """The ``flow`` writes of an ``UpdateWriter`` result."""
    if not result.flow and not result.flow_removed:
        return None
    return FlowPatch(step=step, writer=writer, set=dict(result.flow), remove=list(result.flow_removed))
