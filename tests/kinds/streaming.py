"""Test kinds that stream model output and call tools, and a driver harness (plan P2-01).

``TalkKind`` streams one scripted model turn inside its node; ``ToolAgentKind`` runs a
``create_agent`` graph with one tool through ``invoke_child``. The real ``llm`` and ``agent``
kinds arrive with P2-02; until then these produce the LangGraph events the driver maps.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_core.tools import ToolException, tool

from tests.fakes.clock import FakeClock
from tests.fakes.scripted_chat_model import ScriptedChatModel, Turn
from tests.kinds.harness import DEFAULT_INPUT, Harness, Recorder, with_checkpointer
from zonic_runtime.config.settings import RuntimeResources
from zonic_runtime.engine.child import invoke_child
from zonic_runtime.engine.context import Deadline
from zonic_runtime.engine.kinds.base import BaseKind
from zonic_runtime.engine.node_template import NodeContext
from zonic_runtime.engine.streaming.source import EventSource
from zonic_runtime.models.blueprint import Blueprint, NodeSpec
from zonic_runtime.models.engine import NodeOutcome, Usage
from zonic_runtime.models.io.node_io import NodeInput
from zonic_runtime.ports.tracer import TraceInfo
from zonic_runtime.run.driver import ActiveRun, RunDriver, RunOutcome, RunStartMeta, TurnFinisher
from zonic_runtime.run.events.bus import RunEventBus, RunStream


class TalkKind(BaseKind):
    """Streams the next scripted turn and answers with its text."""

    def __init__(
        self, spec: NodeSpec, blueprint: Blueprint, recorder: Recorder, model: ScriptedChatModel
    ) -> None:
        self.spec, self.recorder, self.model = spec, recorder, model

    async def execute(self, ctx: NodeContext, node_input: NodeInput) -> NodeOutcome:
        self.recorder.calls.append(self.spec.node_id)
        self.recorder.started.set()
        try:
            message = await self.model.ainvoke([HumanMessage("hello")], config=ctx.config)
        except BaseException:
            self.recorder.cancelled.append(self.spec.node_id)
            raise
        usage = Usage(input_tokens=1, output_tokens=2, total_tokens=3, model_calls=1)
        return NodeOutcome(output={"text": message.text}, activate=["default"], usage=usage)


@tool
def lookup(city: str) -> str:
    """Look up the weather in a city."""
    return f"sunny in {city}"


@tool
def broken(city: str) -> str:
    """Always fails."""
    raise ToolException(f"no weather for {city}")


broken.handle_tool_error = True  # the agent sees an error result, as with a failing MCP tool


class ToolAgentKind(BaseKind):
    """A ``create_agent`` graph with ``lookup`` and ``broken``, run through ``invoke_child``."""

    def __init__(
        self, spec: NodeSpec, blueprint: Blueprint, recorder: Recorder, model: ScriptedChatModel
    ) -> None:
        self.spec, self.recorder = spec, recorder
        self.agent = create_agent(model, tools=[lookup, broken], name=spec.graph_name)

    async def execute(self, ctx: NodeContext, node_input: NodeInput) -> NodeOutcome:
        self.recorder.calls.append(self.spec.node_id)
        final = await invoke_child(
            self.agent, {"messages": [HumanMessage("weather?")]}, ctx.runtime, ctx.config
        )
        return NodeOutcome(output={"text": final["messages"][-1].text}, activate=["default"])


def talk(script: Sequence[Turn]) -> tuple[ScriptedChatModel, Any]:
    model = ScriptedChatModel(script=list(script))
    return model, lambda recorder: lambda s, b: TalkKind(s, b, recorder, model)


@dataclass
class DriverRun:
    outcome: RunOutcome
    events: list[dict[str, Any]]
    stream: RunStream

    def types(self) -> list[str]:
        return [e["type"] for e in self.events]

    def text(self, node_id: str | None = None) -> str:
        return "".join(
            e["data"]["text"]
            for e in self.events
            if e["type"] == "token.delta" and (node_id is None or e.get("nodeId") == node_id)
        )


@dataclass
class DriverHarness:
    """Plans, compiles and drives a document the way the run executor will (P2-05). Root graphs
    always have a checkpointer (the ``TurnSaver``, P2-04); until then an ``InMemorySaver``."""

    harness: Harness
    source: EventSource | None = None
    finisher: TurnFinisher | None = None
    clock: FakeClock = field(default_factory=FakeClock)
    #: A ``RunTracer`` (P2-07); each run gets its trace, callbacks and ``RunContext.tracer``.
    tracer: Any = None

    def __post_init__(self) -> None:
        if self.harness.checkpointer is None:
            self.harness = with_checkpointer(self.harness)

    @property
    def resources(self) -> RuntimeResources:
        return self.harness.resources

    def bus(self) -> RunEventBus:
        settings = self.resources.settings
        return RunEventBus(settings.stream, settings.sse.max_event_bytes, clock=self.clock)

    def prepare(
        self,
        document: dict[str, Any],
        *,
        run_id: str = "run-1",
        start_seq: int = 0,
        seconds: float | None = None,
        bus: RunEventBus | None = None,
    ) -> tuple[ActiveRun, Any]:
        blueprint = self.harness.plan(document)
        graph = self.harness.compile(blueprint)
        context = self.harness.context(blueprint, run_id)
        if seconds is not None:
            context = replace(context, deadline=Deadline.after(seconds))
        stream = (bus or self.bus()).create(run_id, start_seq)
        meta = RunStartMeta(
            agentic_workflow_id=blueprint.workflow_id, revision=blueprint.revision, session_id="s-1"
        )
        config = self.harness.config(blueprint, run_id)
        if self.tracer is not None:
            info = TraceInfo(
                run_id=run_id,
                name="test workflow",
                session_id="s-1",
                user_id="owner",
                tags=["wf", "test"],
                blueprint=blueprint,
            )
            trace = self.tracer.begin(info)
            context = replace(context, tracer=trace)
            config = {**config, "callbacks": [*config.get("callbacks", []), *trace.callbacks]}
        run = ActiveRun(context=context, config=config, stream=stream, meta=meta)
        return run, graph

    def driver(self) -> RunDriver:
        return RunDriver(self.resources, source=self.source, finisher=self.finisher, clock=self.clock)

    async def drive(self, run: ActiveRun, graph: Any, payload: Any = None) -> DriverRun:
        outcome = await self.driver().drive(run, graph, dict(DEFAULT_INPUT) if payload is None else payload)
        events = [e async for e in run.stream.subscribe(1)]
        return DriverRun(outcome, events, run.stream)

    async def run(self, document: dict[str, Any], **kwargs: Any) -> DriverRun:
        run, graph = self.prepare(document, **kwargs)
        return await self.drive(run, graph)
