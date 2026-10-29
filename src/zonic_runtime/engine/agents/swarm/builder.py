"""The Swarm subgraph (design 8.6; plan P4-01; cases G-06, W-01 to W-13).

One graph per Swarm node spec, compiled on the node's first visit and kept by its kind:

* ``SwarmState``: the task, the shared ``messages`` (the handoff pairs), the active member, the
  handoffs, the step, the members that finished their steps (``history``), each member's own
  input (``briefs``, resolved per visit by the kind) and what the steps produced (answer, usage,
  turn messages, flow changes).
* One node per member, named by the member's graph name and typed by its metadata, so its steps
  are node observations and its events find their place. It runs the member's agent graph (LLM
  members are agents with no tools of their own) through ``invoke_child``, under its own
  ``TimeoutPolicy`` (``nodeTimeoutSeconds``).
* ``START`` goes to the entry member; a member hands off with a transfer tool (its
  ``Command.PARENT`` routes this graph); a member that answers without one ends the Swarm.

Member steps and handoffs are ``swarm.member.started``, ``swarm.member.finished`` and
``swarm.handoff`` events of the Swarm node; a handoff is also an event observation under the
member's. ``beforeMemberCall`` (on the member) may cancel a step: the Swarm fails with
``SWARM_MEMBER_CANCELLED``. ``onHandoff`` (on the Swarm node) sees each handoff.
"""

from __future__ import annotations

import asyncio
import operator
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphInterrupt, ParentCommand
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime
from langgraph.types import TimeoutPolicy

from zonic_runtime.engine.agents.errors import provider_errors
from zonic_runtime.engine.agents.messages import read_visit, visit_messages
from zonic_runtime.engine.agents.middleware.hooks import CallRun
from zonic_runtime.engine.agents.swarm.handoff_tools import MEMBER_FLOW, MEMBER_MESSAGES, SwarmMemberRef
from zonic_runtime.engine.agents.swarm.member_context import member_messages
from zonic_runtime.engine.child import invoke_child
from zonic_runtime.engine.context import RunContext
from zonic_runtime.errors import ZonicError
from zonic_runtime.models.blueprint import NodeSpec
from zonic_runtime.models.engine import Usage
from zonic_runtime.models.resources.catalogs import ModelCatalog
from zonic_runtime.models.resources.settings import SwarmSettings

MEMBER_STARTED, MEMBER_FINISHED, HANDOFF = "swarm.member.started", "swarm.member.finished", "swarm.handoff"
CANCELLED_CODE = "SWARM_MEMBER_CANCELLED"
_COMPLETED, _FAILED, _CANCELLED = "completed", "failed", "cancelled"
_HANDED_OFF, _STOPPED = "handed_off", "stopped"
_CANCEL = "cancel"


def _merge(old: dict[str, Any] | None, new: dict[str, Any] | None) -> dict[str, Any]:
    return {**(old or {}), **(new or {})}


def _add_usage(old: dict[str, Any] | None, new: dict[str, Any] | None) -> dict[str, Any] | None:
    if not new:
        return old
    if not old:
        return new
    return (Usage.model_validate(old) + Usage.model_validate(new)).model_dump()


class SwarmState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    task: str
    active_member: str
    step: int
    handoffs: Annotated[list[dict[str, Any]], operator.add]
    #: Names of the members that finished their steps, in order: ``nodeHistory``.
    history: Annotated[list[str], operator.add]
    #: Steps per member node ID: the visit number of a member step's observation.
    visits: Annotated[dict[str, int], _merge]
    #: Member node ID -> its own input (``member_context.brief``).
    briefs: dict[str, dict[str, Any]]
    answer: str
    final_member: str
    usage: Annotated[dict[str, Any] | None, _add_usage]
    #: The steps' ``ThreadMessage``s, as the Swarm node's.
    turn_messages: Annotated[list[dict[str, Any]], operator.add]
    #: ``flow`` changes the members' tools made; later members' tools see them in ``zonic_flow``.
    flow_updates: Annotated[dict[str, Any], _merge]
    zonic_flow: Annotated[dict[str, Any], _merge]
    zonic_system: dict[str, Any]
    #: The Swarm node's visit.
    zonic_visit: int


@dataclass(frozen=True)
class SwarmMember:
    ref: SwarmMemberRef
    spec: NodeSpec
    #: The member's compiled agent graph, with a transfer tool for every other member.
    agent: Any
    timeout_seconds: float


@dataclass(frozen=True)
class SwarmSpec:
    """What every member node shares: the Swarm node, its members and the context settings."""

    node: NodeSpec
    members: tuple[SwarmMemberRef, ...]
    settings: SwarmSettings
    #: Member steps per visit; tool idempotency keys number the steps of every visit apart.
    steps: int
    #: The model catalog that costs each member step's usage.
    prices: ModelCatalog | None = None


class MemberNode:
    def __init__(self, member: SwarmMember, swarm: SwarmSpec) -> None:
        self.member, self.swarm = member, swarm
        self.others = tuple(m for m in swarm.members if m.node_id != member.ref.node_id)

    def emit(self, runtime: Runtime[RunContext], visit: int, event: str, **data: Any) -> None:
        node = self.swarm.node
        payload = {"nodeId": node.node_id, "nodeType": node.node_type, "name": node.name, "visit": visit}
        member = {} if event == HANDOFF else {"member": self.member.ref.name}  # a handoff names both
        data = {k: v for k, v in data.items() if v is not None}
        runtime.stream_writer({"event": event, **payload, **member, **data})

    async def __call__(
        self, state: SwarmState, config: RunnableConfig, runtime: Runtime[RunContext]
    ) -> dict[str, Any]:
        ref, run = self.member.ref, runtime.context
        visit, step = int(state.get("zonic_visit") or 1), int(state.get("step") or 1)
        runner = getattr(getattr(run, "hooks", None), "runner", None)
        if runner is not None:
            before = await runner.invoke(
                "beforeMemberCall",
                CallRun(run),
                spec=self.member.spec,
                call={"member": ref.name, "step": step},
            )
            if before.action == _CANCEL:  # case K-13
                self.emit(runtime, visit, MEMBER_FINISHED, status=_CANCELLED)
                raise ZonicError(
                    CANCELLED_CODE,
                    node_id=self.swarm.node.node_id,
                    member=ref.name,
                    reason=before.message or "",
                )
        self.emit(runtime, visit, MEMBER_STARTED)
        started = time.monotonic()
        messages = member_messages(
            (state.get("briefs") or {}).get(ref.node_id) or {}, state, ref, self.others, self.swarm.settings
        )
        # Idempotency keys (DR-2): every step of every visit of the Swarm is its own tool-call visit.
        call_visit = (visit - 1) * self.swarm.steps + step
        member_input = {
            "messages": messages,
            "zonic_flow": dict(state.get("zonic_flow") or {}),
            "zonic_system": dict(state.get("zonic_system") or {}),
            "zonic_visit": call_visit,
            "zonic_swarm": {
                "member": ref.name,
                "step": step,
                "history": list(state.get("history") or []),
                "inputs": len(messages),
            },
        }
        run.tool_calls.start(ref.node_id, call_visit)
        record = {"visits": {ref.node_id: (state.get("visits") or {}).get(ref.node_id, 0) + 1}}
        ended = _FAILED
        try:
            async with provider_errors(ref.node_id):
                final = await invoke_child(self.member.agent, member_input, runtime, config)
            ended = _COMPLETED
        except ParentCommand as handoff:  # a transfer tool: record the step, then let it route
            ended = _HANDED_OFF
            command = handoff.args[0]
            update = dict(command.update or {})
            new = update.pop(MEMBER_MESSAGES, [])
            changes = update.pop(MEMBER_FLOW, {})
            update.update(self.step(record, new, "", visit, changes))
            [entry] = update["handoffs"]
            self.emit(
                runtime,
                visit,
                MEMBER_FINISHED,
                status=_COMPLETED,
                durationMs=_ms(started),
                usage=_wire(update),
            )
            await self.handed_off(runtime, config, visit, entry)
            handoff.args = (replace(command, update=update),)
            raise
        except (GraphInterrupt, asyncio.CancelledError):
            # A pause, or a cancellation (the run's, or the member's timeout: the kind reports
            # that one, since a cancelled step's events no longer reach the stream).
            ended = _STOPPED
            raise
        finally:
            if ended == _FAILED:
                self.emit(runtime, visit, MEMBER_FINISHED, status=_FAILED, durationMs=_ms(started))
        new = list(final["messages"][len(messages) :])
        answer = read_visit(new, self.swarm.prices).text
        done = self.step(record, new, answer, visit, final.get("zonic_flow_updates") or {})
        self.emit(
            runtime, visit, MEMBER_FINISHED, status=_COMPLETED, durationMs=_ms(started), usage=_wire(done)
        )
        return {**done, "answer": answer, "final_member": ref.name}

    def step(
        self,
        record: Mapping[str, Any],
        new: Sequence[Any],
        answer: str,
        visit: int,
        changes: Mapping[str, Any],
    ) -> dict[str, Any]:
        """What a finished step adds to the Swarm state."""
        found = read_visit(new, self.swarm.prices)
        turn = visit_messages(new, answer, self.swarm.node.node_id, visit)
        return {
            **record,
            "history": [self.member.ref.name],
            "usage": found.usage.model_dump() if found.usage else None,
            "turn_messages": [m.model_dump(mode="json") for m in turn],
            "flow_updates": dict(changes),
            "zonic_flow": dict(changes),
        }

    async def handed_off(
        self, runtime: Runtime[RunContext], config: RunnableConfig, visit: int, entry: Mapping[str, Any]
    ) -> None:
        run = runtime.context
        data = {"from": entry.get("from"), "to": [entry.get("to")], "message": entry.get("message") or None}
        self.emit(runtime, visit, HANDOFF, **data)
        tracer = getattr(run, "tracer", None)
        if tracer is not None:
            tracer.event(
                config, HANDOFF, "DEFAULT", {"nodeId": self.swarm.node.node_id, "visit": visit, **data}
            )
        runner = getattr(getattr(run, "hooks", None), "runner", None)
        if runner is not None:
            await runner.invoke(
                "onHandoff", CallRun(run), spec=self.swarm.node, call={"from": data["from"], "to": data["to"]}
            )


def _ms(started: float) -> float:
    return max(0.0, (time.monotonic() - started) * 1000)


def _wire(update: Mapping[str, Any]) -> dict[str, Any] | None:
    usage = update.get("usage")
    return Usage.model_validate(usage).wire() if usage else None


def build_swarm(name: str, swarm: SwarmSpec, members: Sequence[SwarmMember], entry: SwarmMember) -> Any:
    """The compiled Swarm graph; ``name`` is its member blueprint's ``graph_id``, so the tracer
    and the event mapper recognise its member nodes. ``checkpointer=None``: it checkpoints into
    the run's saver under the Swarm node's namespace, so an interrupt in a member resumes there."""
    builder: StateGraph[Any, Any, Any, Any] = StateGraph(SwarmState, context_schema=RunContext)
    for member in members:
        action: Any = MemberNode(member, swarm)
        builder.add_node(
            member.spec.graph_name,
            action,
            timeout=TimeoutPolicy(run_timeout=member.timeout_seconds),
            destinations=tuple(m.spec.graph_name for m in members if m is not member) or None,
            metadata=member.spec.trace_metadata(),
        )
    builder.add_edge(START, entry.spec.graph_name)
    return builder.compile(checkpointer=None, name=name)
