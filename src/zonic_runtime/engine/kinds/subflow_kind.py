"""The ``subflow`` kind (node type ``workflow``; design 6.6; plan P3-09, cases N-11, G-05, S-04, F-10).

The child workflow comes from the loader and the blueprint cache, its graph from the graph cache
in the ``child`` role (compiled with ``checkpointer=None``, DR-3), and it runs through
``invoke_child`` (DR-6), so the parent's callbacks, checkpoint namespace, ``RunContext``, deadline
and permits reach it: its events carry their nested ``path`` and its observations nest under
this node's. The child starts from:

* ``input``: the node's mapped ``inputs``, read by the child's Start node as interface inputs;
* ``system`` and the chat ``history`` of the parent;
* ``flow`` per ``subflow.inheritFlow``: nothing, the variables the child declares, or all.

The child's result (its Output node, else the result fallback chain) is this node's output, with
the child's last chat answer as ``text`` when the result has none (so the parent's fallback finds
the answer of a child whose Output maps something else);
only this node's ``outputStructure`` and ``variableUpdates`` change the parent's ``flow``. The
child's chat messages and node visits join the parent's turn (visits with the nested ``path`` of
node IDs, as on events).
A workflow already on the call chain is ``SUBFLOW_CYCLE``; a chain deeper than ``subflow.maxDepth``
is ``SUBFLOW_DEPTH_EXCEEDED``. Container kind: it holds no node permit while the child runs.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import Any, ClassVar

from zonic_runtime.engine.child import invoke_child
from zonic_runtime.engine.kinds.flow_kinds import _Kind
from zonic_runtime.engine.kinds.io_kinds import mapping_record
from zonic_runtime.engine.node_template import NodeContext
from zonic_runtime.engine.result import answer_text, run_result
from zonic_runtime.engine.state import SEQ, keyed_values
from zonic_runtime.engine.turn_input import turn_input
from zonic_runtime.errors import ZonicError
from zonic_runtime.models.engine import NodeOutcome, ThreadMessage, TimelineEntry, Usage
from zonic_runtime.models.io.node_io import NodeInput

#: The workflows on the call chain, root first, in the child's ``configurable``.
LINEAGE = "zonic_lineage"
_INPUTS = "inputs"
NO_OUTPUT = "NO_OUTPUT"


def inherited(flow: Mapping[str, Any], declared: Collection[str], policy: str) -> dict[str, Any]:
    """``flow`` as ``subflow.inheritFlow`` gives it: none, the child's declared variables, or all."""
    if policy == "none":
        return {}
    if policy == "all":
        return dict(flow)
    return {name: value for name, value in flow.items() if name in declared}


def last_answer(messages: Sequence[ThreadMessage]) -> str | None:
    """The text of the child's last chat answer: what the conversation shows as its reply."""
    for message in reversed(messages):
        if message.role == "assistant" and message.visibility == "chat":
            texts = [p["text"] for p in message.content_parts if isinstance(p.get("text"), str)]
            return "\n".join(texts) if texts else message.text_preview or None
    return None


class SubflowKind(_Kind):
    holds_permit: ClassVar[bool] = False

    async def execute(self, ctx: NodeContext, node_input: NodeInput) -> NodeOutcome:
        node_id = self.spec.node_id
        subflows = ctx.services.ports.subflows
        if subflows is None:
            raise ZonicError("RESOURCE_MISSING", resource="subflows", node_id=node_id)
        child_id = str(ctx.config_values().get("agentic_workflow_id") or "").strip()
        if not child_id:
            raise ZonicError("WORKFLOW_NOT_FOUND", node_id=node_id, agentic_workflow_id="")
        configurable = dict(ctx.config.get("configurable") or {})
        lineage = list(configurable.get(LINEAGE) or [self.blueprint.workflow_id])
        if child_id in lineage:
            raise ZonicError("SUBFLOW_CYCLE", node_id=node_id, path=[*lineage, child_id])
        settings = ctx.services.settings.subflow
        if len(lineage) > settings.max_depth:
            raise ZonicError(
                "SUBFLOW_DEPTH_EXCEEDED", node_id=node_id, depth=len(lineage), limit=settings.max_depth
            )

        child = await subflows.open(self.blueprint, child_id, ctx.run.documents)
        ctx.run.children.add(self.blueprint, node_id, child.blueprint)  # its events find their place
        child_input = turn_input(
            system=keyed_values(ctx.state.get("system")),
            flow=inherited(keyed_values(ctx.state.get("flow")), child.variables, settings.inherit_flow),
            history=ctx.state.get("history") or None,
            inputs=mapping_record(node_input.get(_INPUTS)),
        )
        config = {**ctx.config, "configurable": {**configurable, LINEAGE: [*lineage, child_id]}}
        final = await invoke_child(child.graph, child_input, ctx.runtime, config)  # type: ignore[arg-type]

        try:
            fallback = ctx.services.settings.result.fallback
            result: dict[str, Any] = dict(run_result(final, child.blueprint, fallback).value)
        except ZonicError as error:
            if error.code != NO_OUTPUT:
                raise
            result = {}
        prefix = node_id  # paths are node IDs, as the events' ``path``
        visits = []
        for entry in final.get("timeline") or []:
            plain = {k: v for k, v in entry.items() if k != SEQ}
            nested = plain.get("path")
            visits.append(
                TimelineEntry.model_validate({**plain, "path": f"{prefix}/{nested}" if nested else prefix})
            )
        messages = [
            ThreadMessage.model_validate({k: v for k, v in m.items() if k != SEQ})
            for m in final.get("turn_messages") or []
            if isinstance(m, Mapping) and "role" in m
        ]
        # The child's answer, for the parent's result fallback: its last chat answer, else its
        # result's only string field. A result with neither stays as it is; the fallback then
        # takes it whole.
        answer = last_answer(messages) or answer_text(result)
        if answer and not isinstance(result.get("text"), str):
            result = {**result, "text": answer}
        usage = Usage.model_validate(final["usage"]) if final.get("usage") else None
        return NodeOutcome(
            output={"result": result},
            activate=self.default(ctx),
            usage=usage,
            messages=messages,
            timeline=visits,
        )
