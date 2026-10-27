"""``WorkflowNodeRunner``: the one node template every compiled node runs (design 8.1; plan P1-10).

The 11 steps of design 8.1, in order: open the visit; interrupt first (interrupt kinds);
``node.started``; bind inputs; ``beforeNode`` hooks; execute under the run-wide permit;
build the output; ``afterNode`` hooks; updates; ``node.finished``; route. Failures are not
caught here: an exception from steps 4 to 9 propagates, LangGraph applies the node's
``RetryPolicy``, then its ``error_handler`` (``engine/error_policy.py``). The interrupt
exception is never caught.

The runner and its kind are built once per compiled graph and hold no run data; the run is
reached through ``runtime.context`` and ``config`` only (rule 14).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt

from zonic_runtime.config.settings import RuntimeResources
from zonic_runtime.engine.context import RunContext
from zonic_runtime.engine.kinds.base import NodeKind
from zonic_runtime.engine.patches import flow_patch, to_update, update_messages
from zonic_runtime.engine.routing import command
from zonic_runtime.engine.scope import template_scope
from zonic_runtime.engine.state import RUN_VISITS, RunState
from zonic_runtime.errors import ZonicError
from zonic_runtime.io.output_mapper import OutputMapper
from zonic_runtime.io.port_binder import PortBinder
from zonic_runtime.io.schema_registry import SchemaRegistry
from zonic_runtime.models.blueprint import Blueprint, NodeSpec
from zonic_runtime.models.engine import NodeOutcome, NodeRecord, StatePatch, StateUpdate, TimelineEntry
from zonic_runtime.models.io.node_io import NodeInput, NodeOutput
from zonic_runtime.models.markers import TemplateField, marked_values
from zonic_runtime.models.resources.settings import RuntimeSettings
from zonic_runtime.workflow.templates.conditions import ConditionEvaluator
from zonic_runtime.workflow.templates.resolver import (
    OUTPUT_FIELD,
    TemplateContext,
    TemplateIssues,
    TemplateNode,
    TemplateResolver,
    node_lookup,
)
from zonic_runtime.workflow.templates.updates import UpdateWriter, build_mapping

NODE_STARTED = "node.started"
NODE_FINISHED = "node.finished"
LOOP_ITERATION = "loop.iteration"


class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


@dataclass(frozen=True)
class HookDecision:
    """What a ``beforeNode`` hook decided: changed input, or a skip with its own outcome."""

    node_input: NodeInput | None = None
    skip: NodeOutcome | None = None


class NodeHooks(Protocol):
    """The hook points of the template (design 13; P2-06 implements them). Raising fails the node."""

    async def before_node(self, ctx: NodeContext, node_input: NodeInput) -> HookDecision | None: ...

    async def after_node(self, ctx: NodeContext, outcome: NodeOutcome) -> NodeOutcome | None: ...

    async def on_node_error(self, ctx: NodeContext, error: BaseException) -> NodeOutcome | None: ...


@dataclass(frozen=True)
class NodeServices:
    """Process-wide services of the template; nothing here belongs to a run."""

    resolver: TemplateResolver
    evaluator: ConditionEvaluator
    schemas: SchemaRegistry
    binder: PortBinder
    mapper: OutputMapper
    writer: UpdateWriter
    settings: RuntimeSettings
    resources: RuntimeResources
    clock: Clock = field(default_factory=SystemClock)

    @classmethod
    def from_resources(
        cls, resources: RuntimeResources, schemas: SchemaRegistry | None = None, clock: Clock | None = None
    ) -> NodeServices:
        schemas = schemas or SchemaRegistry(
            resources.manifest, resources.builder, resources.settings.caches.validators
        )
        resolver = TemplateResolver(resources.templates)
        evaluator = ConditionEvaluator(resources.dialects.canonical_operators)
        writer = UpdateWriter(
            resolver,
            evaluator,
            writable=resources.templates.writable,
            writable_system_keys=resources.settings.templates.writable_system_keys,
            state_ops=resources.builder.state_ops,
        )
        return cls(
            resolver=resolver,
            evaluator=evaluator,
            schemas=schemas,
            binder=PortBinder(schemas),
            mapper=OutputMapper(schemas, resources.settings.io.output_validation),
            writer=writer,
            settings=resources.settings,
            resources=resources,
            clock=clock or SystemClock(),
        )


@dataclass
class NodeContext:
    """One node visit: the spec, the state snapshot, the run (through ``runtime``)."""

    spec: NodeSpec
    blueprint: Blueprint
    services: NodeServices
    runtime: Runtime[RunContext]
    config: RunnableConfig
    state: Mapping[str, Any]
    step: int
    visit: int
    started_at: datetime
    started: float
    nodes: Callable[[str], TemplateNode | None]
    interrupt_answer: Any = None
    issues: TemplateIssues = field(default_factory=TemplateIssues)

    @property
    def run(self) -> RunContext:
        return self.runtime.context

    def template_context(self, node_output: Mapping[str, Any] | None = None) -> TemplateContext:
        identity = self.run.run if self.runtime.context is not None else None
        run = (
            {}
            if identity is None
            else {
                "runId": identity.run_id,
                "sessionId": identity.session_id,
                "workflowId": identity.workflow_id,
                "revision": identity.revision,
                "mode": identity.mode,
            }
        )
        scope = template_scope(self.state, run=run, run_metadata=run, interrupt_answer=self.interrupt_answer)
        return TemplateContext(
            scope=scope, nodes=self.nodes, node_id=self.spec.node_id, node_output=node_output
        )

    def resolve(self, value: Any, node_output: Mapping[str, Any] | None = None) -> Any:
        return self.services.resolver.resolve(value, self.template_context(node_output), self.issues)

    def read(self, path: str) -> Any:
        return self.services.resolver.read(path, self.template_context(), self.issues)

    def config_values(self) -> dict[str, Any]:
        """The configuration with its template fields resolved and every other field as written
        (case C-02, M-08)."""
        values = dict(self.spec.config)
        for name, value in marked_values(self.spec.config, TemplateField).items():
            values[name] = self.resolve(value)
        return values

    def emit(self, event: str, **data: Any) -> None:
        payload = {
            "event": event,
            "nodeId": self.spec.node_id,
            "nodeType": self.spec.node_type,
            "name": self.spec.name,
            "visit": self.visit,
            **data,
        }
        self.runtime.stream_writer(payload)


class WorkflowNodeRunner:
    def __init__(self, spec: NodeSpec, blueprint: Blueprint, kind: NodeKind, services: NodeServices) -> None:
        self.spec, self.blueprint, self.kind, self.services = spec, blueprint, kind, services
        self._nodes = node_lookup(blueprint, services.resources.manifest)

    def open(
        self, state: Mapping[str, Any], config: RunnableConfig, runtime: Runtime[RunContext]
    ) -> NodeContext:
        """Step 1: count the visit (the limits are P1-11's)."""
        clock = self.services.clock
        return NodeContext(
            spec=self.spec,
            blueprint=self.blueprint,
            services=self.services,
            runtime=runtime,
            config=config,
            state=state,
            step=int((config.get("metadata") or {}).get("langgraph_step", 0)),
            visit=(state.get("visits") or {}).get(self.spec.node_id, 0) + 1,
            started_at=clock.now(),
            started=clock.monotonic(),
            nodes=self._nodes,
        )

    def check_limits(self, ctx: NodeContext) -> None:
        """Step 1: visit and step limits, from the superstep's snapshot plus this visit (DR-7)."""
        limits = self.blueprint.limits
        if ctx.visit > limits.max_visits_per_node:
            raise ZonicError(
                "LOOP_LIMIT_EXCEEDED",
                node_id=self.spec.node_id,
                limit="maxVisitsPerNode",
                value=limits.max_visits_per_node,
            )
        steps = (ctx.state.get("visits") or {}).get(RUN_VISITS, 0) + 1
        if steps > limits.max_steps:
            raise ZonicError(
                "LOOP_LIMIT_EXCEEDED", node_id=self.spec.node_id, limit="maxSteps", value=limits.max_steps
            )
        loop = self.blueprint.loop_entry(self.spec.node_id)
        if loop is not None and ctx.visit > 1:
            ctx.emit(LOOP_ITERATION, loopId=loop.loop_id, iteration=ctx.visit)

    async def __call__(
        self, state: RunState, config: RunnableConfig, runtime: Runtime[RunContext]
    ) -> Command[Any]:
        ctx = self.open(state, config, runtime)
        self.check_limits(ctx)
        hooks: NodeHooks | None = runtime.context.hooks if runtime.context is not None else None
        if self.kind.interrupts_first:  # 2. before any side effect
            request = self.kind.request(ctx)  # type: ignore[attr-defined]
            ctx.interrupt_answer = interrupt(request.wire())
        ctx.emit(NODE_STARTED, resumed=ctx.interrupt_answer is not None)  # 3
        node_input = self.services.binder.bind(  # 4
            self.spec.node_id, self.spec.io, self._config_dict(), ctx.visit, ctx.resolve, ctx.read
        )
        decision = await hooks.before_node(ctx, node_input) if hooks is not None else None  # 5
        if decision is not None and decision.skip is not None:
            outcome = decision.skip
        else:
            node_input = decision.node_input if decision is not None and decision.node_input else node_input
            if self.kind.holds_permit and runtime.context is not None:  # 6
                async with runtime.context.permits.node():
                    outcome = await self.kind.execute(ctx, node_input)
            else:
                outcome = await self.kind.execute(ctx, node_input)
        output, warnings = self.build_output(ctx, outcome)  # 7
        if hooks is not None:  # 8
            outcome = (
                await hooks.after_node(ctx, outcome.model_copy(update={OUTPUT_FIELD: output.data}))
            ) or outcome
        return self.finish(ctx, outcome, output, warnings)  # 9 to 11

    def _config_dict(self) -> dict[str, Any]:
        return self.spec.config.model_dump(by_alias=True, exclude_none=True)

    def build_output(self, ctx: NodeContext, outcome: NodeOutcome) -> tuple[NodeOutput, list[dict[str, Any]]]:
        """Step 7: the type's output map, ``outputStructure``, then the effective schema."""
        data = self.services.mapper.data(self.spec.io, outcome.output)
        structure = self.spec.config.output_structure or []
        if structure:
            data.update(
                build_mapping(structure, self.services.resolver, ctx.template_context(data), ctx.issues)
            )
        output, issues = self.services.mapper.build(
            self.spec.node_id, self.spec.output_schema, data, usage=outcome.usage
        )
        warnings = [
            {
                "code": "OUTPUT_SCHEMA_INVALID",
                "nodeId": self.spec.node_id,
                "path": i.path,
                "message": i.message,
            }
            for i in issues
        ]
        return output, warnings

    def finish(
        self, ctx: NodeContext, outcome: NodeOutcome, output: NodeOutput, warnings: list[dict[str, Any]]
    ) -> Command[Any]:
        """Steps 9 to 11: updates, ``node.finished`` and the timeline entry, then the route."""
        config = self._config_dict()
        updates: list[Any] = []
        write_to = config.get("writeTo")
        if isinstance(write_to, str) and write_to:  # outputKey / outputVariable (case B-10)
            updates.append(StateUpdate(path=write_to, op="set", value=output.data))
        for key in ("variableUpdates", "stateUpdates"):
            updates += config.get(key) or []
        written = self.services.writer.apply(updates, ctx.template_context(output.data))
        missing = [*ctx.issues.missing, *written.missing]
        warnings = [*warnings, *outcome.warnings, *written.warnings]
        if missing:
            warnings.append(
                {"code": "TEMPLATE_MISSING", "nodeId": self.spec.node_id, "paths": sorted(set(missing))}
            )
        duration = max(0.0, (self.services.clock.monotonic() - ctx.started) * 1000)
        handles = list(outcome.activate)
        record = NodeRecord(
            node_id=self.spec.node_id,
            node_type=self.spec.node_type,
            name=self.spec.name,
            status=outcome.status,
            visit=ctx.visit,
            output=output.data,
            usage=outcome.usage,
        )
        timeline = TimelineEntry(
            node_id=self.spec.node_id,
            node_type=self.spec.node_type,
            visit=ctx.visit,
            status=outcome.status,
            started_at=ctx.started_at,
            duration_ms=duration,
            handles=handles,
        )
        patch = StatePatch(
            flow=flow_patch(written, ctx.step, self.spec.node_id),
            system=written.system,
            node=record,
            messages=[*outcome.messages, *update_messages(written, self.spec.node_id, ctx.visit)],
            timeline=[timeline],
            datasets=outcome.datasets,
            result=outcome.result,
            warnings=warnings,
            usage=outcome.usage,
        )
        update = to_update(
            patch,
            step=ctx.step,
            writer=self.spec.node_id,
            writable_system_keys=self.services.settings.templates.writable_system_keys,
            cleared_threads=written.cleared_threads,
        )
        ctx.emit(
            NODE_FINISHED,
            status=outcome.status,
            durationMs=duration,
            handles=handles,
            usage=outcome.usage.wire() if outcome.usage else None,
        )
        return command(self.blueprint, self.spec, handles, update)


def runner_factory(kinds: Any, services: NodeServices) -> Callable[[NodeSpec, Blueprint], WorkflowNodeRunner]:
    """The compiler's ``runner_factory``: one runner per spec, its kind from the registry."""

    def create(spec: NodeSpec, blueprint: Blueprint) -> WorkflowNodeRunner:
        return WorkflowNodeRunner(spec, blueprint, kinds.create(spec, blueprint), services)

    return create
