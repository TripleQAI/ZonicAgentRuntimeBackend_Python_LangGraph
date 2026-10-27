"""Test kinds and a harness that compiles and runs documents through the real node template
(plan P1-10: the conformance suite C-01 to C-16 and the template cases).

Every kind here keeps its per-test observations in a ``Recorder`` passed at construction
(test-side, never run data): a kind instance itself must hold nothing of a run (C-15).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar

from langgraph.checkpoint.memory import InMemorySaver

from tests.kinds.scripted_kind import run_context
from zonic_runtime.config.settings import RuntimeResources
from zonic_runtime.engine.child import invoke_child
from zonic_runtime.engine.error_policy import error_handler
from zonic_runtime.engine.invocation import invocation_config
from zonic_runtime.engine.kinds.base import BaseKind
from zonic_runtime.engine.kinds.registry import KindRegistry
from zonic_runtime.engine.node_template import NodeContext, NodeServices, runner_factory
from zonic_runtime.errors import ZonicError
from zonic_runtime.models.blueprint import Blueprint, NodeSpec
from zonic_runtime.models.engine import InterruptRequest, NodeOutcome, Usage
from zonic_runtime.models.io.node_io import NodeInput
from zonic_runtime.workflow.blueprint.builder import BlueprintBuilder
from zonic_runtime.workflow.compiler.workflow_compiler import WorkflowCompiler
from zonic_runtime.workflow.loader.loader import WorkflowLoader


@dataclass
class Recorder:
    calls: list[str] = field(default_factory=list)
    inputs: dict[str, Any] = field(default_factory=dict)
    cancelled: list[str] = field(default_factory=list)
    hooks: list[tuple[str, str]] = field(default_factory=list)
    attempts: dict[str, int] = field(default_factory=dict)
    started: asyncio.Event = field(default_factory=asyncio.Event)


class PassKind(BaseKind):
    def __init__(self, spec: NodeSpec, blueprint: Blueprint) -> None:
        self.spec = spec

    async def execute(self, ctx: NodeContext, node_input: NodeInput) -> NodeOutcome:
        return NodeOutcome(activate=[] if self.spec.terminal else ["default"])


class EchoKind(BaseKind):
    """Returns its inputs and resolved configuration; ``fields`` spread into open outputs."""

    def __init__(
        self,
        spec: NodeSpec,
        blueprint: Blueprint,
        recorder: Recorder,
        activate: Sequence[str] = ("default",),
        raw: Mapping[str, Any] | None = None,
        usage: Usage | None = None,
    ) -> None:
        self.spec, self.recorder, self.activate, self.raw, self.usage = (
            spec,
            recorder,
            list(activate),
            raw,
            usage,
        )

    async def execute(self, ctx: NodeContext, node_input: NodeInput) -> NodeOutcome:
        self.recorder.calls.append(self.spec.node_id)
        self.recorder.inputs[self.spec.node_id] = dict(node_input.all())
        raw = (
            dict(self.raw)
            if self.raw is not None
            else {
                "text": f"echo {self.spec.node_id}",
                "fields": {"config": ctx.config_values(), "ports": dict(node_input.all())},
            }
        )
        return NodeOutcome(output=raw, activate=[] if self.spec.terminal else self.activate, usage=self.usage)


class FailingKind(BaseKind):
    """Fails its first ``failures`` attempts with ``code``, then echoes."""

    def __init__(
        self, spec: NodeSpec, blueprint: Blueprint, recorder: Recorder, code: str, failures: int = 10**6
    ) -> None:
        self.spec, self.recorder, self.code, self.failures = spec, recorder, code, failures

    async def execute(self, ctx: NodeContext, node_input: NodeInput) -> NodeOutcome:
        attempt = self.recorder.attempts.get(self.spec.node_id, 0) + 1
        self.recorder.attempts[self.spec.node_id] = attempt
        if attempt <= self.failures:
            raise ZonicError(self.code, attempt=attempt)
        return NodeOutcome(output={"text": "recovered"}, activate=["default"])


class SlowKind(BaseKind):
    def __init__(self, spec: NodeSpec, blueprint: Blueprint, recorder: Recorder, seconds: float) -> None:
        self.spec, self.recorder, self.seconds = spec, recorder, seconds

    async def execute(self, ctx: NodeContext, node_input: NodeInput) -> NodeOutcome:
        self.recorder.calls.append(self.spec.node_id)
        self.recorder.started.set()
        try:
            await asyncio.sleep(self.seconds)
        except asyncio.CancelledError:
            self.recorder.cancelled.append(self.spec.node_id)
            raise
        return NodeOutcome(output={"text": "slow"}, activate=["default"])


class AskKind(BaseKind):
    """Interrupts first; the answer becomes its output."""

    interrupts_first: ClassVar[bool] = True

    def __init__(self, spec: NodeSpec, blueprint: Blueprint, recorder: Recorder) -> None:
        self.spec, self.recorder = spec, recorder

    def request(self, ctx: NodeContext) -> InterruptRequest:
        return InterruptRequest(kind="input", node_id=self.spec.node_id, prompt="answer?")

    async def execute(self, ctx: NodeContext, node_input: NodeInput) -> NodeOutcome:
        self.recorder.calls.append(self.spec.node_id)
        return NodeOutcome(output={"value": ctx.interrupt_answer}, activate=["default"])


class ResultKind(BaseKind):
    """An Output: sets the run result."""

    def __init__(self, spec: NodeSpec, blueprint: Blueprint, recorder: Recorder) -> None:
        self.spec, self.recorder = spec, recorder

    async def execute(self, ctx: NodeContext, node_input: NodeInput) -> NodeOutcome:
        self.recorder.calls.append(self.spec.node_id)
        return NodeOutcome(output={"answer": self.spec.node_id}, result={"answer": self.spec.node_id})


class RecordingHooks:
    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder

    async def before_node(self, ctx: NodeContext, node_input: NodeInput) -> None:
        self.recorder.hooks.append(("before", ctx.spec.node_id))

    async def after_node(self, ctx: NodeContext, outcome: NodeOutcome) -> None:
        self.recorder.hooks.append(("after", ctx.spec.node_id))

    async def on_node_error(self, ctx: NodeContext, error: BaseException) -> None:
        self.recorder.hooks.append(("error", ctx.spec.node_id))


Factory = Callable[[NodeSpec, Blueprint], Any]
#: Every run starts with a user query (the API's ``system.userQuery``), which agent ports bind.
DEFAULT_INPUT: dict[str, Any] = {
    "system": {"values": {"userQuery": "hello"}, "provenance": {}, "conflicts": []}
}


@dataclass
class Harness:
    resources: RuntimeResources
    kinds: dict[str, Factory]
    recorder: Recorder = field(default_factory=Recorder)
    checkpointer: Any = None
    hooks: Any = None

    def plan(self, document: dict[str, Any]) -> Blueprint:
        loader = WorkflowLoader.from_resources(self.resources)
        return BlueprintBuilder(self.resources).build(loader.from_stored(document, "wf"))

    def compile(self, blueprint: Blueprint) -> Any:
        default = {k: PassKind for k in {s.kind for s in blueprint.nodes.values()}}
        registry = KindRegistry({**default, **self.kinds})
        services = NodeServices.from_resources(self.resources)
        compiler = WorkflowCompiler(
            runner_factory(registry, services),
            self.resources.settings.engine,
            error_handler_factory=error_handler,
            saver=self.checkpointer,
        )
        return compiler.compile(blueprint, role="root" if self.checkpointer else "child")

    def context(self, blueprint: Blueprint, run_id: str = "run-1") -> Any:
        ctx = run_context(blueprint, self.resources, run_id)
        return replace(ctx, hooks=self.hooks) if self.hooks is not None else ctx

    def config(self, blueprint: Blueprint, run_id: str = "run-1") -> dict[str, Any]:
        context = self.context(blueprint, run_id)
        return invocation_config(context.run, self.resources.settings.engine, environment="test")

    async def run(
        self, document: dict[str, Any], inputs: Any = None, run_id: str = "run-1"
    ) -> tuple[dict[str, Any], list[dict[str, Any]], Blueprint, Any]:
        blueprint = self.plan(document)
        graph = self.compile(blueprint)
        events: list[dict[str, Any]] = []
        final: dict[str, Any] = {}
        async for mode, chunk in graph.astream(
            inputs if inputs is not None else dict(DEFAULT_INPUT),
            config=self.config(blueprint, run_id),
            context=self.context(blueprint, run_id),
            stream_mode=["custom", "values"],
        ):
            if mode == "custom":
                events.append(chunk)
            else:
                final = chunk
        return final, events, blueprint, graph


def with_checkpointer(harness: Harness) -> Harness:
    return replace(harness, checkpointer=InMemorySaver())


class ChildKind(BaseKind):
    """Runs a one-node child graph through ``invoke_child`` and returns what the child saw (G-14)."""

    def __init__(self, spec: NodeSpec, blueprint: Blueprint, recorder: Recorder, child_graph: Any) -> None:
        self.spec, self.recorder, self.child_graph = spec, recorder, child_graph

    async def execute(self, ctx: NodeContext, node_input: NodeInput) -> NodeOutcome:
        final = await invoke_child(self.child_graph, {}, ctx.runtime, ctx.config)
        return NodeOutcome(output={"child": final.get("seen")}, activate=["default"])


#: ``(spec, visit, blueprint) -> handles``.
Policy = Callable[[NodeSpec, int, Blueprint], Sequence[str]]


class PolicyKind(BaseKind):
    """Activates what a policy chooses; agents count as model calls in the recorder."""

    def __init__(self, spec: NodeSpec, blueprint: Blueprint, recorder: Recorder, policy: Policy) -> None:
        self.spec, self.blueprint, self.recorder, self.policy = spec, blueprint, recorder, policy

    async def execute(self, ctx: NodeContext, node_input: NodeInput) -> NodeOutcome:
        self.recorder.calls.append(self.spec.node_id)
        handles = list(self.policy(self.spec, ctx.visit, self.blueprint))
        # Shaped like an entry node's output too: platform start nodes copy
        # ``interface.inputs.message`` into ``system.userQuery``, which agent prompts bind.
        output = {"text": f"{self.spec.node_id}#{ctx.visit}", "interface": {"inputs": {"message": "hello"}}}
        return NodeOutcome(output=output, activate=handles)


def answer_after(
    rounds: int, handoff_prefix: str = "handoff-", default_rule_ids: Sequence[str] = ("r_else", "r-else")
) -> Policy:
    """Agents hand off on their first ``rounds`` visits, then answer; rules take their default
    branch; every other node follows ``default`` (or its only connected handle)."""

    def policy(spec: NodeSpec, visit: int, blueprint: Blueprint) -> Sequence[str]:
        connected = blueprint.edge_index.handles(spec.node_id)
        if not connected or spec.terminal:
            return []
        handoffs = [h for h in connected if h.startswith(handoff_prefix)]
        if handoffs and visit <= rounds:
            return [handoffs[(visit - 1) % len(handoffs)]]
        if handoffs:
            return [h for h in connected if not h.startswith(handoff_prefix)][:1]
        defaults = [h for h in default_rule_ids if h in connected]
        if defaults:
            return defaults[:1]
        return ["default"] if "default" in connected else connected[:1]

    return policy


def policy_kinds(blueprint_kinds: Sequence[str], recorder: Recorder, policy: Policy) -> dict[str, Factory]:
    return {kind: (lambda s, b: PolicyKind(s, b, recorder, policy)) for kind in blueprint_kinds}


def seek_loop(handoff_prefix: str = "handoff-") -> Policy:
    """Every node takes the handle that leads soonest into (or around) a loop: agents always
    hand off, rules pick the branch that stays in the loop. The run can only end at a limit."""

    def distance_to_loops(blueprint: Blueprint) -> dict[str, int]:
        looped = {n for c in blueprint.components for n in c.nodes}
        reverse: dict[str, list[str]] = {}
        for source, by_handle in blueprint.edge_index.edges.items():
            for node_ids in by_handle.values():
                for target in node_ids:
                    reverse.setdefault(target, []).append(source)
        distance = dict.fromkeys(looped, 0)
        frontier = list(looped)
        while frontier:
            current = frontier.pop(0)
            for source in reverse.get(current, []):
                if source not in distance:
                    distance[source] = distance[current] + 1
                    frontier.append(source)
        return distance

    def policy(spec: NodeSpec, visit: int, blueprint: Blueprint) -> Sequence[str]:
        distance = distance_to_loops(blueprint)
        connected = blueprint.edge_index.handles(spec.node_id)
        if not connected or spec.terminal:
            return []

        def score(handle: str) -> tuple[int, int]:
            targets = blueprint.edge_index.targets(spec.node_id, handle)
            nearest = min((distance.get(t, 10**6) for t in targets), default=10**6)
            return (nearest, 0 if handle.startswith(handoff_prefix) else 1)

        return [min(connected, key=score)]

    return policy
