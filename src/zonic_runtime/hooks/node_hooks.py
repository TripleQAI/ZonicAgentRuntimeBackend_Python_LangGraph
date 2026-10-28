"""``TemplateHooks``: the hook runner at the node template's, the driver's and the agent
middleware's hook points (design 13; plan P2-06).

* ``beforeNode`` (template step 5): a changed input is revalidated against its ports
  (``HOOK_RESULT_INVALID``, case K-04); ``skip`` completes the node with the hook's output and
  handles; ``fail`` fails the node with the hook's code (``fail_run``: the run).
* ``afterNode`` (step 8): a transformed output, added warnings, handles with ``canRoute``.
* ``onNodeError`` (``ErrorPolicyHandler``): ``retry`` (bounded by ``hooks.maxRetriesPerNode``),
  ``recover`` with an output and handles, or ``fail``, which escalates to the run (case K-06).
* ``beforeRun``, ``afterRun``, ``onRunError`` (``RunDriver``): reject the run, set variables,
  transform the result, notify.

Hooks read the run through a ``HookRun`` over the visit's (or the turn's) template scope.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from zonic_runtime.engine.approvals import approval_request
from zonic_runtime.engine.error_policy import FAIL_RUN
from zonic_runtime.engine.errors import error_code
from zonic_runtime.engine.kinds.subflow_kind import LINEAGE
from zonic_runtime.engine.node_template import ErrorDecision, HookDecision, NodeContext, NodeServices
from zonic_runtime.engine.scope import template_scope
from zonic_runtime.engine.state import keyed_values
from zonic_runtime.engine.turn_input import keyed
from zonic_runtime.errors import ZonicError
from zonic_runtime.hooks.runner import INVALID, HookOutcome, HookRunner
from zonic_runtime.models.blueprint import Blueprint
from zonic_runtime.models.engine import InterruptRequest, NodeOutcome
from zonic_runtime.models.io.node_io import NodeInput
from zonic_runtime.models.nodes.base import ConditionGroup
from zonic_runtime.runtime.redact import StreamMaskRules
from zonic_runtime.workflow.templates.resolver import (
    OUTPUT_FIELD,
    TemplateContext,
    TemplateIssues,
    node_lookup,
)


class _Schemas:
    def __init__(self, services: NodeServices) -> None:
        self._services = services

    def validate(self, schema_id: str, value: Any) -> list[str]:
        schemas = self._services.schemas
        if not schemas.has(schema_id):
            raise ZonicError(INVALID, reason=f"unknown schema {schema_id}")
        return [
            f"{i.path or '(root)'}: {i.message}" for i in schemas.validate(schemas.schema(schema_id), value)
        ]


class NodeHookRun(_Schemas):
    """A node visit as hooks read it."""

    def __init__(self, ctx: NodeContext) -> None:
        super().__init__(ctx.services)
        self._ctx = ctx
        #: Warnings the hooks raised (``warn``); the template adds them to the node's.
        self.warnings: list[dict[str, Any]] = []

    @property
    def path(self) -> str:
        configurable = self._ctx.config.get("configurable") or {}
        return "/".join(configurable.get(LINEAGE) or [])

    def warn(self, code: str, message: str, node_id: str | None = None) -> None:
        self.warnings.append({"code": code, "message": message, "nodeId": node_id or self._ctx.spec.node_id})

    def approval(self, binding_id: str) -> Any:
        """The answer to an ``approval`` binding's request at this visit, if it was asked."""
        return self._ctx.approvals.get(binding_id)

    def scope_id(self, scope: str) -> str | None:
        run = self._ctx.run.run
        return {
            "run": run.run_id,
            "session": run.session_id,
            "user": run.owner_sub,
            "tenant": run.tenant_id,
        }.get(scope)

    def trace_hook(self, name: str, level: str, metadata: Mapping[str, Any], *, span: bool) -> None:
        tracer = self._ctx.run.tracer
        if tracer is None:
            return
        found = {"nodeId": self._ctx.spec.node_id, "visit": self._ctx.visit, **metadata}
        (tracer.span if span else tracer.event)(self._ctx.config, name, level, found)

    def annotate(
        self, tags: Sequence[str], metadata: Mapping[str, Any], score: Mapping[str, Any] | None
    ) -> None:
        if self._ctx.run.tracer is not None:
            self._ctx.run.tracer.annotate(self._ctx.config, tags, metadata, score)

    @property
    def run_id(self) -> str:
        return self._ctx.run.run.run_id

    @property
    def blueprint(self) -> Blueprint:
        return self._ctx.blueprint

    def resolve(self, value: Any, node_output: Mapping[str, Any] | None = None) -> Any:
        return self._ctx.resolve(value, node_output)

    def holds(self, when: ConditionGroup, node_output: Mapping[str, Any] | None = None) -> bool:
        return self._ctx.services.evaluator.all(
            when.conditions, when.condition_logic, lambda v: self._ctx.resolve(v, node_output)
        )


class TurnHookRun(_Schemas):
    """The turn as run-level hooks read it: the turn's state before the first node or after the last."""

    def __init__(
        self,
        services: NodeServices,
        blueprint: Blueprint,
        run_id: str,
        state: Mapping[str, Any],
        tracer: Any = None,
    ) -> None:
        super().__init__(services)
        self._services, self._blueprint, self._run_id, self._state = services, blueprint, run_id, state
        self._tracer = tracer
        self._nodes = node_lookup(blueprint, services.resources.manifest)
        self.warnings: list[dict[str, Any]] = []

    @property
    def path(self) -> str:
        return ""

    def warn(self, code: str, message: str, node_id: str | None = None) -> None:
        self.warnings.append({"code": code, "message": message, **({"nodeId": node_id} if node_id else {})})

    def scope_id(self, scope: str) -> str | None:
        system = keyed_values(self._state.get("system"))
        return {"run": self._run_id, "session": system.get("sessionId")}.get(scope)

    def trace_hook(self, name: str, level: str, metadata: Mapping[str, Any], *, span: bool) -> None:
        return None  # run-level hook observations: the run's own observation records the outcome

    def annotate(
        self, tags: Sequence[str], metadata: Mapping[str, Any], score: Mapping[str, Any] | None
    ) -> None:
        if self._tracer is not None:
            self._tracer.annotate(None, tags, metadata, score)

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def blueprint(self) -> Blueprint:
        return self._blueprint

    def _context(self, node_output: Mapping[str, Any] | None) -> TemplateContext:
        return TemplateContext(
            scope=template_scope(self._state), nodes=self._nodes, node_id=None, node_output=node_output
        )

    def resolve(self, value: Any, node_output: Mapping[str, Any] | None = None) -> Any:
        return self._services.resolver.resolve(value, self._context(node_output), TemplateIssues())

    def holds(self, when: ConditionGroup, node_output: Mapping[str, Any] | None = None) -> bool:
        return self._services.evaluator.all(
            when.conditions, when.condition_logic, lambda v: self.resolve(v, node_output)
        )


def _fail(outcome: HookOutcome, node_id: str | None = None) -> ZonicError:
    details: dict[str, Any] = {"node_id": node_id} if node_id else {}
    if outcome.fail_run:
        details[FAIL_RUN] = True
    return ZonicError(outcome.code or "HOOK_FAILED", **details)


class TemplateHooks:
    def __init__(self, runner: HookRunner, services: NodeServices) -> None:
        self.runner = runner
        self.services = services

    # -- node points (the template's NodeHooks protocol) --------------------------------------

    def approval_requests(self, ctx: NodeContext) -> list[tuple[str, InterruptRequest]]:
        """The node's ``approval`` requests whose ``when`` holds; the template asks them at step 2,
        before any side effect (resolving the prompt is pure)."""
        run = NodeHookRun(ctx)
        found = []
        hours = ctx.services.settings.interrupts.default_expiry_hours
        for item in self.runner.approvals(ctx.blueprint, "beforeNode", ctx.spec.node_id):
            when = item.binding.when
            if when is not None and not run.holds(when):
                continue
            config: Any = item.config
            prompt = ctx.resolve(config.prompt)
            request = approval_request(
                item.binding.id, config, ctx.spec.node_id, prompt, ctx.started_at, hours
            )
            found.append((item.binding.id, request))
        return found

    async def before_node(self, ctx: NodeContext, node_input: NodeInput) -> HookDecision | None:
        run = NodeHookRun(ctx)
        outcome = await self.runner.invoke(
            "beforeNode", run, spec=ctx.spec, visit=ctx.visit, input=dict(node_input.all())
        )
        outcome.warnings += run.warnings
        if outcome.action == "fail":
            raise _fail(outcome, ctx.spec.node_id)
        if outcome.action == "skip":
            handles = outcome.handles or [ctx.services.resources.manifest.defaults.default_handle]
            skip = NodeOutcome(output=outcome.output or {}, activate=handles, mapped=True)
            return HookDecision(skip=skip, updates=outcome.updates, warnings=outcome.warnings)
        changed = None
        if outcome.input is not None:
            changed = NodeInput(ctx.spec.node_id, ctx.visit, outcome.input)
            self._check_input(ctx, changed)
        if changed is None and not outcome.updates and not outcome.warnings:
            return None
        return HookDecision(node_input=changed, updates=outcome.updates, warnings=outcome.warnings)

    @staticmethod
    def _check_input(ctx: NodeContext, node_input: NodeInput) -> None:
        schemas = ctx.services.schemas
        for port in ctx.spec.io.inputs if ctx.spec.io is not None else []:
            value = node_input.get(port.name)
            if value is None:
                if port.required:
                    raise ZonicError(INVALID, node_id=ctx.spec.node_id, port=port.name, reason="required")
                continue
            if schemas.validate(schemas.schema(port.schema_ref), value, f"inputs.{port.name}"):
                raise ZonicError(INVALID, node_id=ctx.spec.node_id, port=port.name, reason="schema")

    async def after_node(self, ctx: NodeContext, outcome: NodeOutcome) -> NodeOutcome | None:
        run = NodeHookRun(ctx)
        found = await self.runner.invoke(
            "afterNode",
            run,
            spec=ctx.spec,
            visit=ctx.visit,
            output=outcome.output,
            handles=list(outcome.activate),
            parts=outcome.parts,
        )
        if found.action == "fail":
            raise _fail(found, ctx.spec.node_id)
        warnings = [*found.warnings, *run.warnings]
        if (
            found.output is None
            and found.handles is None
            and found.parts is None
            and not found.updates
            and not warnings
        ):
            return None
        return outcome.model_copy(
            update={
                OUTPUT_FIELD: found.output if found.output is not None else outcome.output,
                "parts": found.parts if found.parts is not None else outcome.parts,
                "activate": found.handles if found.handles is not None else outcome.activate,
                "updates": [*outcome.updates, *found.updates],
                "warnings": [*outcome.warnings, *warnings],
            }
        )

    async def on_node_error(
        self, ctx: NodeContext, error: BaseException, attempt: int
    ) -> ErrorDecision | None:
        message = str(error)[:500]
        found = await self.runner.invoke(
            "onNodeError",
            NodeHookRun(ctx),
            spec=ctx.spec,
            visit=ctx.visit,
            error={"code": error_code(error), "message": message},
            attempt=attempt,
        )
        if found.action == "fail":
            raise _fail(found, ctx.spec.node_id)
        if found.action == "recover":
            handles = found.handles or [ctx.services.resources.manifest.defaults.default_handle]
            recovered = NodeOutcome(
                output=found.output or {},
                activate=handles,
                mapped=True,
                updates=found.updates,
                warnings=found.warnings,
            )
            return ErrorDecision(recovered=recovered)
        if found.action == "retry":
            return ErrorDecision(retry_after=found.delay_ms / 1000)
        return None

    # -- run points (the driver's RunHooks protocol) -------------------------------------------

    def stream_mask(self, blueprint: Blueprint, node_id: str) -> StreamMaskRules | None:
        return self.runner.stream_mask(blueprint, node_id)

    def _turn(self, blueprint: Blueprint, run_id: str, state: Mapping[str, Any], tracer: Any) -> TurnHookRun:
        return TurnHookRun(self.services, blueprint, run_id, state, tracer)

    async def before_run(
        self, blueprint: Blueprint, run_id: str, payload: dict[str, Any], tracer: Any = None
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """``beforeRun``: ``fail`` rejects the run; ``setVariables`` changes the turn's ``flow``."""
        turn = self._turn(blueprint, run_id, payload, tracer)
        found = await self.runner.invoke("beforeRun", turn)
        if found.action == "fail":
            raise _fail(found)
        found.warnings += turn.warnings
        if not found.updates:
            return payload, found.warnings
        context = TemplateContext(
            scope=template_scope(payload),
            nodes=node_lookup(blueprint, self.services.resources.manifest),
            node_id="",
        )
        written = self.services.writer.apply(found.updates, context)
        flow = {**keyed_values(payload.get("flow")), **written.flow}
        for key in written.flow_removed:
            flow.pop(key, None)
        return {**payload, "flow": keyed(flow)}, [*found.warnings, *written.warnings]

    async def after_run(
        self,
        blueprint: Blueprint,
        run_id: str,
        state: Mapping[str, Any],
        result: dict[str, Any],
        tracer: Any = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        turn = self._turn(blueprint, run_id, state, tracer)
        found = await self.runner.invoke("afterRun", turn, result=result)
        return (found.result if found.result is not None else result), [*found.warnings, *turn.warnings]

    async def on_run_error(
        self,
        blueprint: Blueprint,
        run_id: str,
        state: Mapping[str, Any],
        error: dict[str, str],
        tracer: Any = None,
    ) -> list[dict[str, Any]]:
        turn = self._turn(blueprint, run_id, state, tracer)
        found = await self.runner.invoke("onRunError", turn, error=error)
        return [*found.warnings, *turn.warnings]

    async def on_interrupt(
        self,
        blueprint: Blueprint,
        run_id: str,
        state: Mapping[str, Any],
        interrupts: list[dict[str, Any]],
        tracer: Any = None,
    ) -> list[dict[str, Any]]:
        """``onInterrupt``: the run pauses for ``interrupts`` (``interruptId``, ``nodeId``, ``kind``)."""
        turn = self._turn(blueprint, run_id, state, tracer)
        found = await self.runner.invoke("onInterrupt", turn, call={"interrupts": interrupts})
        return [*found.warnings, *turn.warnings]

    async def on_resume(
        self, blueprint: Blueprint, run_id: str, interrupt_id: str, answer: Any, tracer: Any = None
    ) -> None:
        """``onResume``: an answer about to resume the run; ``fail`` rejects it (the answer is not
        recorded and the run keeps waiting)."""
        turn = self._turn(blueprint, run_id, {}, tracer)
        found = await self.runner.invoke(
            "onResume", turn, call={"interruptId": interrupt_id, "answer": answer}
        )
        if found.action == "fail":
            raise ZonicError(found.code or "INTERRUPT_ANSWER_INVALID", reason=found.message or "")
