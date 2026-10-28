"""``RunDriver``: one turn of a compiled workflow (design Sections 9.1, 9.5, 10; plan P2-01).

The driver publishes ``run.started``, runs the graph through an ``EventSource`` (v3 by
default) with the run's ``config``, ``context``, ``durability`` and ``control``, and turns
how the stream ended into a ``RunOutcome``:

* finished: the result from the Output kind or the fallback chain (``NO_OUTPUT`` otherwise);
* interrupted: ``waiting_input`` with the pending interrupts and the final state;
* ``GraphDrained``: ``drained``;
* the run deadline (``asyncio`` wait with the run's ``Deadline``): ``timed_out``;
* ``ActiveRun.cancel``: ``cancelled``; a node that ignores the cancellation is left behind
  after ``runs.cancelGraceSeconds``;
* any other exception: ``failed`` with its catalog code (``LOOP_LIMIT_EXCEEDED``,
  ``NODE_TIMEOUT``, the failing node's code, or ``INTERNAL``).

The ``TurnFinisher`` then saves the turn (P2-04) and may replace the outcome, for example
with ``STATE_UNAVAILABLE``. Only after it returns are the terminal events published:
``run.completed`` follows a successful save (design 9.3).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol, runtime_checkable

from langgraph.errors import GraphDrained
from langgraph.runtime import RunControl
from langgraph.types import Durability
from pydantic import BaseModel

from zonic_runtime.config.settings import RuntimeResources
from zonic_runtime.engine.context import RunContext
from zonic_runtime.engine.errors import UNKNOWN_CODE, error_code
from zonic_runtime.engine.result import run_result
from zonic_runtime.engine.streaming.mapper import EventMapper, NamespaceMap, StreamMasking
from zonic_runtime.engine.streaming.source import EventSource, StreamedRun
from zonic_runtime.engine.streaming.transformer import V3EventSource
from zonic_runtime.errors import ZonicError
from zonic_runtime.models.blueprint import Blueprint
from zonic_runtime.models.engine import Usage
from zonic_runtime.models.events import EventDraft
from zonic_runtime.run.events.bus import RunStream
from zonic_runtime.run.events.summarizer import summarize
from zonic_runtime.run.interrupt_ids import row_id
from zonic_runtime.runtime.clock import Clock, SystemClock

RunStatus = Literal["succeeded", "failed", "cancelled", "timed_out", "waiting_input", "drained"]
CANCELLED, RUN_TIMEOUT = "CANCELLED", "RUN_TIMEOUT"
WAITING = "waiting_input"
INTERRUPT_REQUESTED, INTERRUPT_ANSWERED = "interrupt.requested", "interrupt.answered"
_DEFAULT_INTERRUPT_KIND = "input"


@dataclass(frozen=True)
class RunError:
    code: str
    message: str
    retryable: bool


@dataclass(frozen=True)
class RunOutcome:
    status: RunStatus
    usage: Usage = field(default_factory=Usage)
    result: dict[str, Any] | None = None
    #: ``outputNode`` or the fallback that produced the result.
    result_source: str | None = None
    error: RunError | None = None
    #: The final state of a finished or paused turn (what the save reads).
    state: dict[str, Any] | None = None
    interrupts: list[Any] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class RunStartMeta:
    agentic_workflow_id: str
    revision: str
    session_id: str | None = None
    title: str | None = None
    #: The interrupt row IDs a resumed turn answers (P4-02): its trace records them.
    answered: tuple[str, ...] = ()


@dataclass
class ActiveRun:
    """One turn on this instance; holds everything the driver needs and nothing compiled."""

    context: RunContext
    config: dict[str, Any]
    stream: RunStream
    meta: RunStartMeta
    durability: Durability = "exit"
    control: RunControl = field(default_factory=RunControl)
    stop_reason: str | None = None
    #: Lost to another instance, or released by a drain: its turn is not saved here (P4-05).
    stopped: bool = False
    _stop: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def run_id(self) -> str:
        return self.context.run.run_id

    def cancel(self, reason: str = CANCELLED) -> None:
        """Stop the run (``POST /v1/runs/{id}/cancel``, the error policy, a hard shutdown)."""
        if self.stop_reason is None:
            self.stop_reason = reason
        self._stop.set()


@runtime_checkable
class RunHooks(Protocol):
    """The run-level hook points (design 13): ``hooks/node_hooks.TemplateHooks`` implements them."""

    async def before_run(
        self, blueprint: Blueprint, run_id: str, payload: dict[str, Any], tracer: Any = None
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]: ...

    async def after_run(
        self,
        blueprint: Blueprint,
        run_id: str,
        state: Mapping[str, Any],
        result: dict[str, Any],
        tracer: Any = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]: ...

    async def on_run_error(
        self,
        blueprint: Blueprint,
        run_id: str,
        state: Mapping[str, Any],
        error: dict[str, str],
        tracer: Any = None,
    ) -> list[dict[str, Any]]: ...

    async def on_interrupt(
        self,
        blueprint: Blueprint,
        run_id: str,
        state: Mapping[str, Any],
        interrupts: list[dict[str, Any]],
        tracer: Any = None,
    ) -> list[dict[str, Any]]: ...


class TurnFinisher(Protocol):
    """Saves the turn before its terminal event; may replace the outcome."""

    async def __call__(self, run: ActiveRun, outcome: RunOutcome) -> RunOutcome: ...


class _Stopped(Exception):
    """The run was stopped from outside (deadline or cancel) before the graph finished."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RunDriver:
    def __init__(
        self,
        resources: RuntimeResources,
        source: EventSource | None = None,
        finisher: TurnFinisher | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._resources = resources
        self._settings = resources.settings
        self._source = source or V3EventSource()
        self._finisher = finisher
        self._clock = clock or SystemClock()

    async def drive(self, run: ActiveRun, graph: Any, payload: Any) -> RunOutcome:
        stream = run.stream
        stream.publish(EventDraft(type="run.started", data=self._started(run.meta)))
        names = NamespaceMap(run.context.blueprint, run.context.children.get)
        hooks = run.context.hooks if isinstance(run.context.hooks, RunHooks) else None
        mapper = EventMapper(names, summarize, monotonic=self._clock.monotonic, masking=self._masking(run))
        trace = run.context.tracer
        with trace.active() if trace is not None else nullcontext():
            if trace is not None and run.meta.answered:  # the same trace across the wait (L-09)
                trace.event(None, INTERRUPT_ANSWERED, metadata={"interruptIds": list(run.meta.answered)})
            outcome = await self._traced(run, graph, payload, mapper, hooks)
            if trace is not None and outcome.status == WAITING:
                identity = run.context.run
                ids = [row_id(identity.run_id, identity.attempt, str(i.id)) for i in outcome.interrupts]
                trace.event(None, INTERRUPT_REQUESTED, metadata={"interruptIds": ids})
            if trace is not None:
                trace.end(outcome.status, outcome.result, outcome.error.code if outcome.error else None)
        if self._finisher is not None:
            try:
                outcome = await self._finisher(run, outcome)
            except ZonicError as error:
                outcome = self._failed(error.code, outcome.usage)
        for warning in outcome.warnings:
            stream.publish(EventDraft(type="warning", data=warning, node_id=warning.get("nodeId")))
        for draft in self._terminal(run, outcome):
            stream.publish(draft)
        stream.close()
        return outcome

    def _masking(self, run: ActiveRun) -> StreamMasking | None:
        """The stream masking of nodes with ``mask`` bindings (P3-10), from the run's hooks."""
        rules = getattr(run.context.hooks, "stream_mask", None)
        if not callable(rules):
            return None
        blueprint, settings = run.context.blueprint, self._settings.hooks
        return StreamMasking(
            rules=lambda node_id: rules(blueprint, node_id),
            patterns=settings.patterns,
            holdback_chars=settings.stream_mask_holdback_chars,
        )

    async def _traced(
        self, run: ActiveRun, graph: Any, payload: Any, mapper: EventMapper, hooks: RunHooks | None
    ) -> RunOutcome:
        warnings: list[dict[str, Any]] = []
        try:
            if hooks is not None and isinstance(payload, dict):  # beforeRun: a new turn's input
                payload, warnings = await hooks.before_run(
                    run.context.blueprint, run.run_id, payload, run.context.tracer
                )
            streamed = await self._run(run, graph, payload, mapper)
            outcome = self._completion(run, streamed, mapper)
        except _Stopped as stopped:
            outcome = self._failed(stopped.code, mapper.usage)
        except GraphDrained:
            outcome = RunOutcome(status="drained", usage=mapper.usage)
        except asyncio.CancelledError:
            if run.stop_reason is None:
                raise  # the driver itself was cancelled: not a run outcome
            outcome = self._failed(run.stop_reason, mapper.usage)
        except Exception as error:  # noqa: BLE001  # principle: allow rule-17-broad-except run-outcome-boundary
            outcome = self._failed(self._code(error), mapper.usage)
        if hooks is not None:
            outcome = await self._run_hooks(hooks, run, outcome)
        if warnings:
            outcome = replace(outcome, warnings=[*warnings, *outcome.warnings])
        return outcome

    async def _run_hooks(self, hooks: RunHooks, run: ActiveRun, outcome: RunOutcome) -> RunOutcome:
        """``afterRun`` (the result built, before the save), ``onRunError`` and ``onInterrupt`` (a
        pause, before its save). A failing ``afterRun`` fails the run; the others only add warnings."""
        state = outcome.state or {}
        blueprint = run.context.blueprint
        try:
            if outcome.status == "succeeded" and outcome.result is not None:
                result, added = await hooks.after_run(
                    blueprint, run.run_id, state, outcome.result, run.context.tracer
                )
                return replace(outcome, result=result, warnings=[*outcome.warnings, *added])
            if outcome.error is not None:
                error = {"code": outcome.error.code, "message": outcome.error.message}
                added = await hooks.on_run_error(blueprint, run.run_id, state, error, run.context.tracer)
                return replace(outcome, warnings=[*outcome.warnings, *added])
            if outcome.status == WAITING:
                identity = run.context.run
                pending = [
                    {
                        k: v
                        for k, v in interrupt_data(i, identity.run_id, identity.attempt).items()
                        if k != "schema"
                    }
                    for i in outcome.interrupts
                ]
                added = await hooks.on_interrupt(blueprint, run.run_id, state, pending, run.context.tracer)
                return replace(outcome, warnings=[*outcome.warnings, *added])
        except ZonicError as error:
            return replace(self._failed(error.code, outcome.usage), state=outcome.state)
        return outcome

    async def _run(self, run: ActiveRun, graph: Any, payload: Any, mapper: EventMapper) -> StreamedRun:
        inner = asyncio.ensure_future(
            self._source.run(
                graph,
                payload,
                config=run.config,
                context=run.context,
                durability=run.durability,
                control=run.control,
                mapper=mapper,
                emit=run.stream.publish,
            )
        )
        stop = asyncio.ensure_future(run._stop.wait())
        try:
            done, _ = await asyncio.wait(
                {inner, stop}, timeout=run.context.deadline.remaining(), return_when=asyncio.FIRST_COMPLETED
            )
            if inner not in done:
                code = run.stop_reason if stop in done and run.stop_reason else RUN_TIMEOUT
                run.stop_reason = run.stop_reason or code
                inner.cancel()
                await asyncio.wait({inner}, timeout=self._settings.runs.cancel_grace_seconds)
                raise _Stopped(code)
            return inner.result()
        finally:
            stop.cancel()
            if not inner.done():
                inner.cancel()

    def _completion(self, run: ActiveRun, streamed: StreamedRun, mapper: EventMapper) -> RunOutcome:
        state = streamed.output or {}
        usage = (
            Usage.model_validate(state["usage"]) if isinstance(state.get("usage"), Mapping) else mapper.usage
        )
        if streamed.interrupts:
            return RunOutcome(
                status="waiting_input", usage=usage, state=state, interrupts=streamed.interrupts
            )
        try:
            result = run_result(state, run.context.blueprint, self._settings.result.fallback)
        except ZonicError as error:
            return replace(self._failed(error.code, usage), state=state)
        return RunOutcome(
            status="succeeded",
            usage=usage,
            result=result.value,
            result_source=result.source,
            state=state,
            warnings=result.warnings,
        )

    @staticmethod
    def _code(error: BaseException) -> str:
        return error_code(error)

    def _error(self, code: str) -> RunError:
        entry = self._resources.errors.errors.get(code)
        if entry is None:
            code, entry = UNKNOWN_CODE, self._resources.errors.errors[UNKNOWN_CODE]
        return RunError(code=code, message=entry.message, retryable=entry.retryable)

    def _failed(self, code: str, usage: Usage) -> RunOutcome:
        status: RunStatus = (
            "cancelled" if code == CANCELLED else "timed_out" if code == RUN_TIMEOUT else "failed"
        )
        return RunOutcome(status=status, usage=usage, error=self._error(code))

    @staticmethod
    def _started(meta: RunStartMeta) -> dict[str, Any]:
        data = {"agenticWorkflowId": meta.agentic_workflow_id, "revision": meta.revision}
        if meta.session_id is not None:
            data["sessionId"] = meta.session_id
        if meta.title is not None:
            data["title"] = meta.title
        return data

    def _terminal(self, run: ActiveRun, outcome: RunOutcome) -> list[EventDraft]:
        if outcome.status == "succeeded":
            data = {"result": outcome.result or {}, "usage": outcome.usage.wire()}
            return [EventDraft(type="run.completed", data=data)]
        if outcome.status == "waiting_input":
            identity = run.context.run
            requested = [interrupt_data(i, identity.run_id, identity.attempt) for i in outcome.interrupts]
            ids = [r["interruptId"] for r in requested]
            nodes = [_node_of(i) for i in outcome.interrupts]
            return [
                *(
                    EventDraft(type="interrupt.requested", data=r, node_id=n)
                    for r, n in zip(requested, nodes, strict=True)
                ),
                EventDraft(type="run.waiting", data={"interruptIds": ids}),
            ]
        if outcome.status == "drained":
            return []  # another instance resumes the run and ends its stream
        if outcome.status == "cancelled":
            return [
                EventDraft(
                    type="run.cancelled",
                    data={"reason": (outcome.error.code if outcome.error else CANCELLED).lower()},
                )
            ]
        error = outcome.error or self._error(UNKNOWN_CODE)
        return [
            EventDraft(
                type="run.failed",
                data={"code": error.code, "message": error.message, "retryable": error.retryable},
            )
        ]


def _node_of(interrupt: Any) -> str | None:
    value = interrupt.value if isinstance(interrupt.value, Mapping) else {}
    node = value.get("nodeId")
    return node if isinstance(node, str) and node else None


def interrupt_data(interrupt: Any, run_id: str, attempt: int) -> dict[str, Any]:
    """``interrupt.requested`` data of a LangGraph ``Interrupt`` (its value is the kind's
    ``InterruptRequest``), under the interrupt's row ID for this pause."""
    value = interrupt.value if isinstance(interrupt.value, Mapping) else {"prompt": str(interrupt.value)}
    schema = value.get("answerSchema")
    if not isinstance(schema, Mapping):
        declared = getattr(interrupt, "response_schema", None)
        schema = (
            declared.model_json_schema()
            if isinstance(declared, type) and issubclass(declared, BaseModel)
            else declared
        )
    data: dict[str, Any] = {
        "interruptId": row_id(run_id, attempt, str(interrupt.id)),
        "kind": str(value.get("kind") or _DEFAULT_INTERRUPT_KIND),
        "schema": dict(schema) if isinstance(schema, Mapping) else {},
    }
    prompt = value.get("prompt")
    if isinstance(prompt, Mapping):
        prompt = prompt.get("text")
    if prompt is not None:
        data["prompt"] = str(prompt)
    if value.get("expiresAt") is not None:
        data["expiresAt"] = str(value["expiresAt"])
    return data
