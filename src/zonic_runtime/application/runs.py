"""The run use cases (design 9.1, 9.5, 14.1; plan P2-03, P4-04): ``StartRun`` in stream, sync and
async modes, ``ResumeRun`` (P4-02), ``CancelRun``, run status, the node timeline and the event feed.
Ported from the Node.js runtime's ``run-service.ts``.

``async`` returns ``202`` with the run's links at once. With durable runs (P4-05, design 9.5):

* every run started here holds this instance's lease (``RunLeases``), and an ``async`` run with no
  free permit is stored ``queued`` for a claim by any instance;
* ``async`` turns run with ``durability="async"`` (SP-10), with write-behind checkpoints when
  ``state.asyncCheckpoints`` is on, and post their completion callback;
* a claimed run continues from its checkpoint (a drain or write-behind, with ``None`` input),
  or runs its turn again from the session's saved state, or fails with ``SHUTDOWN``, as
  ``runs.onInstanceLoss`` says for its mode;
* ``drain`` drains this instance's ``async`` runs to checkpoints and releases them; ``stream`` and
  ``sync`` runs get ``shutdown.drainSeconds`` to finish, then fail with ``SHUTDOWN``.

A run's graph runs in this process as its own task, independent of the request that started it;
its events go to the run's stream, and its turn is saved before the terminal event.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from langgraph.types import Command

from zonic_runtime.application.caller import Caller
from zonic_runtime.application.paging import invalid
from zonic_runtime.application.runtime import Prepared, WorkflowRuntime
from zonic_runtime.application.sessions import SessionService
from zonic_runtime.config.settings import RuntimeResources
from zonic_runtime.engine.context import (
    ChildBlueprints,
    Deadline,
    RunContext,
    RunDocuments,
    RunIdentity,
    RunPermits,
    RunPorts,
)
from zonic_runtime.engine.invocation import invocation_config
from zonic_runtime.engine.persistence.codec import CheckpointBundle
from zonic_runtime.engine.turn_input import turn_input
from zonic_runtime.errors import ZonicError
from zonic_runtime.integrations.gateway.repository import GatewayRepository
from zonic_runtime.integrations.http.run_events_proxy import RunEventsProxy
from zonic_runtime.models.api.requests import StartRunRequest
from zonic_runtime.models.api.responses import NodeVisitList, Run, RunAccepted, RunError, RunLinks, RunResult
from zonic_runtime.models.engine import TimelineEntry, Usage
from zonic_runtime.models.events import EventDraft
from zonic_runtime.models.gateway.runtime import (
    IDEMPOTENCY_KEY_WIDTH,
    ClaimedRunRow,
    RunMode,
    RunReleaseArgs,
    RunRow,
    RunStatus,
    RunTransitionArgs,
)
from zonic_runtime.models.gateway.wire import Filter
from zonic_runtime.ports.datasets import RunDatasets
from zonic_runtime.ports.mcp import RunMcp
from zonic_runtime.ports.tracer import NoopTracer, RunTracer, TraceInfo
from zonic_runtime.run.callbacks import CompletionCallbacks
from zonic_runtime.run.driver import ActiveRun, RunDriver, RunOutcome, RunStartMeta
from zonic_runtime.run.events.bus import RunEventBus, RunStream, envelope
from zonic_runtime.run.executor.claimer import ClaimedRun
from zonic_runtime.run.executor.executor import RunExecutor
from zonic_runtime.run.executor.instance import RunInstance
from zonic_runtime.run.executor.leases import RunLeases
from zonic_runtime.run.registry import RunRegistry
from zonic_runtime.run.state.checkpoint_store import CheckpointStore, LoadedCheckpoint
from zonic_runtime.run.state.turn_store import TurnStateStore
from zonic_runtime.runtime.clock import Clock, SystemClock
from zonic_runtime.runtime.lru import LruCache

TERMINAL = frozenset({"succeeded", "failed", "cancelled", "timed_out"})
_ASYNC = "async"
_CANCELLED = "CANCELLED"
_SHUTDOWN, _LEASE_LOST = "SHUTDOWN", "RUN_LEASE_LOST"
_DRAIN_REASON = "shutdown"
_CALLER = "caller"
_PAUSE = "pause"
_RESTART, _FAIL = "restart_turn", "fail"
#: Admission refusals that queue an ``async`` run instead (P4-05).
_QUEUED_CODES = frozenset({"RATE_LIMITED", "OVERLOADED"})
_STREAM = "stream"
_SYSTEM_PREFIX = "system."
_SPACES = re.compile(r"\s+")
_SESSIONS_SEEN = 10_000

Events = AsyncIterator[dict[str, Any]]


@dataclass(frozen=True)
class DurableRuns:
    """The durable-run collaborators (design 9.5): this instance, the leases of its runs and the
    completion callbacks. A run service without them runs every turn here, unleased."""

    instance: RunInstance
    leases: RunLeases
    callbacks: CompletionCallbacks | None = None


@dataclass(frozen=True)
class StartedStream:
    run_id: str
    session_id: str
    events: Events


StartedRun = StartedStream | RunResult | RunAccepted


class RunService:
    def __init__(
        self,
        resources: RuntimeResources,
        runtime: WorkflowRuntime,
        driver: RunDriver,
        bus: RunEventBus,
        registry: RunRegistry,
        turns: TurnStateStore,
        sessions: SessionService,
        repository: GatewayRepository,
        *,
        clock: Clock | None = None,
        tracer: RunTracer | None = None,
        executor: RunExecutor | None = None,
        new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
        checkpoints: CheckpointStore | None = None,
        durable: DurableRuns | None = None,
    ) -> None:
        self._resources = resources
        self._settings = resources.settings
        self._runtime = runtime
        self._driver = driver
        self._bus = bus
        self._registry = registry
        self._turns = turns
        self._sessions = sessions
        self._repository = repository
        self._clock = clock or SystemClock()
        self._tracer = tracer or NoopTracer()
        self._executor = executor or RunExecutor(resources.settings.runs)
        #: ``(owner, session) -> workflow``: a warm turn reads nothing before its start batch
        #: (case P-05); ``sp_run_start`` still checks the owner of the session.
        self._sessions_seen: LruCache[tuple[str, str], str] = LruCache(_SESSIONS_SEEN)
        self._new_id = new_id
        self._checkpoints = checkpoints
        self.durable = durable
        #: The events of runs other instances drive (P4-05); set with durable runs.
        self.proxy: RunEventsProxy | None = None
        #: Completion callbacks in flight (kept so they are not collected before they finish).
        self._background: set[asyncio.Task[Any]] = set()

    # -- StartRun ------------------------------------------------------------------------

    async def start(
        self, caller: Caller, request: StartRunRequest, idempotency_key: str | None = None
    ) -> StartedRun:
        text = self._valid_input(request, idempotency_key)
        try:
            release = self._executor.admit()  # held until the run's task ends (case R-04)
        except ZonicError as error:  # an async run past the permits waits in the queue (Q-10)
            if request.mode == _ASYNC and self.durable is not None and error.code in _QUEUED_CODES:
                return await self._queue(caller, request, idempotency_key, text)
            raise
        try:
            return await self._admitted(caller, request, idempotency_key, text, release)
        except BaseException:
            release()
            raise

    async def _admitted(
        self,
        caller: Caller,
        request: StartRunRequest,
        idempotency_key: str | None,
        text: str,
        release: Callable[[], None],
    ) -> StartedRun:
        session_id = str(request.session_id) if request.session_id else None
        workflow_id = request.agentic_workflow_id
        if session_id is not None:
            known = await self._session_workflow(caller, session_id)
            if workflow_id is not None and workflow_id != known:
                raise invalid("agenticWorkflowId", "does not match the session's workflow")
            workflow_id = known
        if workflow_id is None:
            raise invalid("agenticWorkflowId", "is required when sessionId is absent")
        # Compiled before the run row exists, so a plan that cannot run never leaves a running row.
        prepared = await self._runtime.prepare(workflow_id, caller)
        run_id = self._new_id()
        title = None if session_id else self._title(text)
        started = await self._turns.begin(
            run_id=run_id,
            owner_sub=caller.subject,
            tenant_id=caller.tenant,
            email=caller.email,
            agentic_workflow_id=workflow_id,
            revision=prepared.loaded.revision,
            document_version=prepared.loaded.version,
            mode=request.mode,
            text=text,
            session_id=session_id,
            title=title,
            idempotency_key=idempotency_key,
            lease=self._lease(),
            caller=self._snapshot(caller) if self.durable is not None else None,
            callback=self._callback(request),
        )
        self._sessions_seen.put((caller.subject, started.session_id), workflow_id)
        if started.replayed:
            release()
            return await self._replay(caller, started.run_id, started.session_id, request.mode)

        identity = RunIdentity(
            run_id=run_id,
            session_id=started.session_id,
            owner_sub=caller.subject,
            tenant_id=caller.tenant,
            workflow_id=workflow_id,
            revision=prepared.loaded.revision,
            mode=request.mode,
        )
        document = prepared.loaded.document
        system = self._system(document, text, run_id, started.session_id)
        variables = document.variable_definitions()
        defaults = {name: v.default for name, v in variables.items() if v.default is not None}
        persisted = [name for name, v in variables.items() if v.persist is not False]
        self._turns.track(run_id, started.state, persisted)
        payload = turn_input(
            system=system,
            flow={**defaults, **started.state.flow},
            history=started.state.history,
            inputs=self._interface_inputs(request),
        )
        documents = RunDocuments()
        documents.add(workflow_id, prepared.loaded.stored)
        meta = RunStartMeta(
            agentic_workflow_id=workflow_id,
            revision=prepared.loaded.revision,
            session_id=started.session_id,
            title=title,
        )
        return await self._launch(
            caller,
            identity,
            prepared,
            payload,
            meta=meta,
            documents=documents,
            release=release,
            text=text,
            wait=request.mode != _ASYNC,
            callback=self._callback(request),
        )

    async def _queue(
        self, caller: Caller, request: StartRunRequest, idempotency_key: str | None, text: str
    ) -> StartedRun:
        """An ``async`` run with no free permit: stored ``queued`` (its plan checked first), claimed by
        an instance with a free permit; ``202`` at once."""
        session_id = str(request.session_id) if request.session_id else None
        workflow_id = (
            await self._session_workflow(caller, session_id) if session_id else request.agentic_workflow_id
        )
        if workflow_id is None:
            raise invalid("agenticWorkflowId", "is required when sessionId is absent")
        prepared = await self._runtime.prepare(workflow_id, caller)
        started = await self._turns.begin(
            run_id=self._new_id(),
            owner_sub=caller.subject,
            tenant_id=caller.tenant,
            email=caller.email,
            agentic_workflow_id=workflow_id,
            revision=prepared.loaded.revision,
            document_version=prepared.loaded.version,
            mode=request.mode,
            text=text,
            session_id=session_id,
            title=None if session_id else self._title(text),
            idempotency_key=idempotency_key,
            queued=True,
            caller=self._snapshot(caller),
            callback=self._callback(request),
        )
        if started.replayed:
            return await self._replay(caller, started.run_id, started.session_id, request.mode)
        return self._accepted(started.run_id, started.session_id, "queued")

    def _lease(self) -> dict[str, Any] | None:
        durable = self.durable
        return durable.instance.lease(self._settings.async_runs.lease_seconds) if durable else None

    @staticmethod
    def _snapshot(caller: Caller) -> dict[str, Any]:
        """Who a claiming instance runs the turn as: the caller's email and groups at the start."""
        return {"email": caller.email or "", "groups": sorted(caller.groups)}

    @staticmethod
    def _callback(request: StartRunRequest) -> dict[str, Any] | None:
        callback = request.options.callback if request.options is not None else None
        return callback.model_dump(by_alias=True) if callback is not None else None

    async def _launch(
        self,
        caller: Caller,
        identity: RunIdentity,
        prepared: Prepared,
        payload: Any,
        *,
        meta: RunStartMeta,
        documents: RunDocuments,
        release: Callable[[], None],
        text: str | None = None,
        bundle: CheckpointBundle | None = None,
        start_seq: int = 0,
        wait: bool = True,
        callback: Mapping[str, Any] | None = None,
        background: bool = False,
    ) -> StartedRun:
        """Runs a turn as its own task: a new one, a paused one resumed from its ``bundle`` (P4-02),
        or a claimed one (``background``, P4-05), whose events continue after ``start_seq``."""
        run_id, workflow_id = identity.run_id, identity.workflow_id
        engine = self._settings.engine
        document = prepared.loaded.document
        children = ChildBlueprints()  # the run's sub-flow children, for its events and its trace
        trace = self._tracer.begin(
            TraceInfo(
                run_id=run_id,
                name=document.display_name or document.name or workflow_id,
                session_id=identity.session_id,
                user_id=caller.subject,
                tags=[document.name or workflow_id, self._settings.observability.environment, identity.mode],
                metadata={
                    "agenticWorkflowId": workflow_id,
                    **({"tenantId": caller.tenant} if caller.tenant else {}),
                    "revision": prepared.loaded.revision,
                    "documentVersion": prepared.loaded.version,
                },
                input={"text": text} if text is not None else {"interrupts": list(meta.answered)},
                blueprint=prepared.blueprint,
                children=children.get,
            )
        )
        context = RunContext(
            run=identity,
            deadline=Deadline.after(engine.run_timeout_seconds, self._clock.monotonic),
            blueprint=prepared.blueprint,
            settings=self._settings,
            permits=RunPermits(engine.max_concurrency_per_run, self._settings.tools.max_concurrent_per_turn),
            ports=self._ports(identity, caller),
            hooks=self._runtime.hooks,
            tracer=trace,
            children=children,
            documents=documents,
        )
        run = ActiveRun(
            context=context,
            config=invocation_config(
                identity,
                engine,
                environment=self._settings.observability.environment,
                callbacks=trace.callbacks,
            ),
            stream=self._bus.create(run_id, start_seq),
            meta=meta,
            # A drained async run continues elsewhere: its child graphs must keep their checkpoints.
            durability="async" if identity.mode == _ASYNC else "exit",
        )
        self._runtime.saver.attach(run_id, bundle)
        if self.durable is not None:
            self.durable.leases.hold(run_id)
        flusher = (
            asyncio.create_task(self._turns.write_behind(run))
            if identity.mode == _ASYNC and self._settings.state.async_checkpoints
            else None
        )
        task = asyncio.create_task(self._driver.drive(run, prepared.graph, payload))
        task.add_done_callback(lambda done: self._ended(run, done, release, flusher, callback))
        self._registry.add(run, task)
        if background:
            return self._accepted(run_id, identity.session_id, "running")
        if identity.mode == _STREAM:
            events = run.stream.subscribe(start_seq + 1)
            return StartedStream(run_id=run_id, session_id=identity.session_id, events=events)
        if not wait:
            return self._accepted(run_id, identity.session_id, "running")
        return await self._await_sync(run_id, identity.session_id)

    async def resume(
        self,
        caller: Caller,
        checkpoint: LoadedCheckpoint,
        answers: Mapping[str, Any],
        mode: RunMode,
        *,
        tenant: str | None = None,
        wait: bool = True,
    ) -> StartedRun:
        """ResumeRun (design 9.4 step 4, Section 15.14): the run moves on to the next attempt (the
        fencing token of its saves), and its turn continues on this instance from the checkpoint:
        the graph from the stored documents, the restored buffer, ``Command(resume={id: answer})``
        for the answered interrupts (row IDs mapped to LangGraph's; the subset rule of SP-8)."""
        run_id, turn = checkpoint.run_id, checkpoint.turn
        prepared = self.prepared(checkpoint)
        workflow_id = prepared.loaded.agentic_workflow_id
        release = self._executor.admit()
        try:
            patch = {"ownerSub": caller.subject, "attempt": checkpoint.attempt, "newAttempt": True}
            transition = RunTransitionArgs(
                run_id=UUID(run_id), to="running", patch={**patch, **(self._lease() or {})}
            )
            await self._repository.call(transition)
            identity = RunIdentity(
                run_id=run_id,
                session_id=str(turn.get("sessionId") or ""),
                owner_sub=caller.subject,
                tenant_id=caller.tenant or tenant,
                workflow_id=workflow_id,
                revision=prepared.loaded.revision,
                mode=mode,
                attempt=checkpoint.attempt + 1,
            )
            started, persisted, saved = TurnStateStore.resumed_facts(turn)
            self._turns.track(run_id, started, persisted, saved)
            resume = {checkpoint.interrupts.get(rid, rid): answer for rid, answer in answers.items()}
            meta = RunStartMeta(
                agentic_workflow_id=workflow_id,
                revision=prepared.loaded.revision,
                session_id=identity.session_id,
                answered=tuple(answers),
            )
            return await self._launch(
                caller,
                identity,
                prepared,
                Command(resume=resume),
                meta=meta,
                documents=RunDocuments(checkpoint.documents),
                release=release,
                bundle=checkpoint.bundle,
                start_seq=checkpoint.last_event_seq,
                wait=wait and mode != _ASYNC,
            )
        except BaseException:
            release()
            raise

    def prepared(self, checkpoint: LoadedCheckpoint) -> Prepared:
        """A paused run's graph, compiled from the root document it paused with (H-09)."""
        workflow_id = str(checkpoint.turn.get("agenticWorkflowId") or "")
        stored = checkpoint.documents.get(workflow_id)
        if stored is None:
            raise ZonicError("CHECKPOINT_INVALID", run_id=checkpoint.run_id, reason="no root document")
        return self._runtime.prepare_stored(stored, workflow_id)

    def _ended(
        self,
        run: ActiveRun,
        task: asyncio.Task[RunOutcome],
        release: Callable[[], None],
        flusher: asyncio.Task[None] | None,
        callback: Mapping[str, Any] | None,
    ) -> None:
        """A turn's task ended: its permit, lease and write-behind go; a finished run's completion
        callback is sent in the background."""
        release()
        if flusher is not None:
            flusher.cancel()
        durable = self.durable
        if durable is not None:
            durable.leases.drop(run.run_id)
        if task.cancelled() or task.exception() is not None or run.stopped:
            return
        outcome = task.result()
        if callback is None or durable is None or durable.callbacks is None or outcome.status not in TERMINAL:
            return
        summary: dict[str, Any] = {
            "runId": run.run_id,
            "sessionId": run.context.run.session_id,
            "status": outcome.status,
            "finishedAt": self._clock.now().isoformat(),
        }
        if outcome.result is not None:
            summary["result"] = outcome.result
        if outcome.error is not None:
            summary["error"] = {"code": outcome.error.code, "retryable": outcome.error.retryable}
        sent = asyncio.create_task(durable.callbacks.send(callback, summary))
        self._background.add(sent)
        sent.add_done_callback(self._background.discard)

    # -- claimed runs, lost runs, drain (P4-05) ---------------------------------------------------

    async def run_claimed(self, claim: ClaimedRun) -> None:
        """A run this instance claimed (design 9.5 steps 2 and 4): a queued run starts; a run whose
        instance stopped follows ``runs.onInstanceLoss`` for its mode. Either continues from its
        checkpoint (a drain, write-behind), else runs its turn again from the saved state."""
        durable = self.durable
        if durable is None:
            return
        row = await self._repository.read_one(
            ClaimedRunRow, filters=[Filter(column="run_id", value=claim.run_id)]
        )
        if row is None:
            return
        policies = self._settings.runs.on_instance_loss.model_dump(by_alias=True)
        policy = policies.get(claim.mode, _FAIL)
        if claim.previous_status == "running" and policy != _RESTART:
            await self._fail_claimed(claim, _SHUTDOWN)
            return
        try:
            release = self._executor.admit()
        except ZonicError:  # the permits went to other runs meanwhile: back to the queue
            await self._repository.call(
                RunReleaseArgs(instance_id=durable.instance.id, run_ids=[claim.run_id])
            )
            return
        caller = self._caller_of(row)
        try:
            checkpoint = (
                await self._checkpoints.load(claim.run_id, row.owner_sub)
                if row.has_checkpoint and self._checkpoints is not None
                else None
            )
            if checkpoint is not None and checkpoint.kind != _PAUSE:
                await self._continue(caller, checkpoint, claim, release, row.callback)
            else:
                await self._restart(caller, row, claim, release)
        except ZonicError as error:
            release()
            durable.leases.drop(claim.run_id)
            await self._fail_claimed(claim, error.code)

    @staticmethod
    def _caller_of(row: ClaimedRunRow) -> Caller:
        """Who a claimed run runs as: its owner, with the snapshot ``sp_run_start`` kept."""
        found = row.input.get(_CALLER)
        snapshot: dict[str, Any] = found if isinstance(found, dict) else {}
        return Caller(
            subject=row.owner_sub,
            tenant=row.tenant_id or None,
            email=snapshot.get("email") or None,
            groups=frozenset(snapshot.get("groups") or []),
        )

    async def _continue(
        self,
        caller: Caller,
        checkpoint: LoadedCheckpoint,
        claim: ClaimedRun,
        release: Callable[[], None],
        callback: Mapping[str, Any] | None,
    ) -> None:
        """A drained (or written-behind) turn goes on from its checkpoint with ``None`` input: the
        supersteps it completed do not run again (R-03)."""
        turn = checkpoint.turn
        prepared = self.prepared(checkpoint)
        identity = RunIdentity(
            run_id=claim.run_id,
            session_id=str(turn.get("sessionId") or claim.session_id),
            owner_sub=caller.subject,
            tenant_id=caller.tenant,
            workflow_id=prepared.loaded.agentic_workflow_id,
            revision=prepared.loaded.revision,
            mode=_ASYNC if claim.mode == _ASYNC else claim.mode,  # type: ignore[arg-type]
            attempt=claim.attempt,
        )
        started, persisted, saved = TurnStateStore.resumed_facts(turn)
        self._turns.track(claim.run_id, started, persisted, saved)
        meta = RunStartMeta(
            agentic_workflow_id=identity.workflow_id,
            revision=identity.revision,
            session_id=identity.session_id,
        )
        await self._launch(
            caller,
            identity,
            prepared,
            None,
            meta=meta,
            documents=RunDocuments(checkpoint.documents),
            release=release,
            bundle=checkpoint.bundle,
            start_seq=checkpoint.last_event_seq,
            callback=callback,
            background=True,
        )

    async def _restart(
        self, caller: Caller, row: ClaimedRunRow, claim: ClaimedRun, release: Callable[[], None]
    ) -> None:
        """A claimed turn run again from the session's saved state (design 9.5 step 4), with the same
        idempotency keys for its tool calls (DR-2)."""
        prepared = await self._runtime.prepare(row.agentic_workflow_id, caller)
        session_id = str(row.session_id)
        started = await self._turns.restart(
            run_id=claim.run_id,
            session_id=session_id,
            agentic_workflow_id=row.agentic_workflow_id,
            owner_sub=row.owner_sub,
            attempt=claim.attempt,
        )
        text = str(row.input.get("text") or "")
        document = prepared.loaded.document
        variables = document.variable_definitions()
        defaults = {name: v.default for name, v in variables.items() if v.default is not None}
        self._turns.track(
            claim.run_id, started.state, [n for n, v in variables.items() if v.persist is not False]
        )
        identity = RunIdentity(
            run_id=claim.run_id,
            session_id=session_id,
            owner_sub=row.owner_sub,
            tenant_id=row.tenant_id or None,
            workflow_id=row.agentic_workflow_id,
            revision=prepared.loaded.revision,
            mode=row.mode,
            attempt=claim.attempt,
        )
        payload = turn_input(
            system=self._system(document, text, claim.run_id, session_id),
            flow={**defaults, **started.state.flow},
            history=started.state.history,
            inputs=self._interface_inputs(
                StartRunRequest.model_validate(
                    {"input": {k: v for k, v in row.input.items() if k != _CALLER}, "mode": row.mode}
                )
            ),
        )
        documents = RunDocuments()
        documents.add(row.agentic_workflow_id, prepared.loaded.stored)
        meta = RunStartMeta(
            agentic_workflow_id=row.agentic_workflow_id, revision=identity.revision, session_id=session_id
        )
        await self._launch(
            caller,
            identity,
            prepared,
            payload,
            meta=meta,
            documents=documents,
            release=release,
            text=text,
            callback=row.callback,
            background=True,
        )

    async def _fail_claimed(self, claim: ClaimedRun, code: str) -> None:
        """A claimed run that cannot run on: ``failed`` with ``code``, fenced by the claim's attempt."""
        error = self._catalog_error(code)
        patch = {"attempt": claim.attempt, "error": {"code": error.code, "message": error.message}}
        with contextlib.suppress(ZonicError):
            await self._repository.call(
                RunTransitionArgs(run_id=UUID(claim.run_id), to="failed", patch=patch)
            )

    def lost(self, run_id: str) -> None:
        """A run another instance took over (its heartbeat said so, Q-13): stopped here, not saved."""
        active = self._registry.get(run_id)
        if active is not None:
            active.stopped = True
            active.cancel(_LEASE_LOST)

    async def drain(self, seconds: float) -> None:
        """A graceful shutdown (design 9.5, Section 15.15): every ``async`` run here drains at its next
        superstep boundary, is saved there and released to the queue; ``stream`` and ``sync`` runs
        get ``seconds`` to finish, then fail with ``SHUTDOWN``; waiting runs are untouched (R-03)."""
        for run in self._registry.runs():
            if run.context.run.mode == _ASYNC:
                run.control.request_drain(_DRAIN_REASON)
        await self._registry.settle(seconds)
        for run in self._registry.runs():
            run.cancel(_SHUTDOWN)
        await self._registry.settle(self._settings.runs.cancel_grace_seconds)

    def _interface_inputs(self, request: StartRunRequest) -> dict[str, Any]:
        """A root run's interface inputs from the request (``dialects.json`` ``requestInputs``)."""
        fields = request.input.model_dump(exclude_none=True)
        return {
            name: fields[field]
            for name, field in self._resources.dialects.request_inputs.items()
            if field in fields
        }

    def _ports(self, identity: RunIdentity, caller: Caller) -> RunPorts:
        """The run's owner-bound ports (design 7.1, case T-08)."""
        mcp = self._runtime.services.ports.mcp
        bound = (
            RunMcp(mcp, identity.run_id, identity.owner_sub, identity.session_id, caller.tenant)
            if mcp is not None
            else None
        )
        ports = self._runtime.services.ports
        datasets = RunDatasets(ports.datasets, identity.run_id) if ports.datasets is not None else None
        return RunPorts(mcp=bound, secrets=ports.secrets, datasets=datasets)

    async def _session_workflow(self, caller: Caller, session_id: str) -> str:
        known = self._sessions_seen.get((caller.subject, session_id))
        if known is None:
            known = (await self._sessions.row(caller, session_id)).agentic_workflow_id
            self._sessions_seen.put((caller.subject, session_id), known)
        return known

    def _valid_input(self, request: StartRunRequest, idempotency_key: str | None) -> str:
        text = request.input.text
        if not text.strip():
            raise invalid("input.text", "must not be empty")
        if len(text) > self._settings.input.max_chars:
            raise ZonicError("INPUT_TOO_LARGE", max_chars=self._settings.input.max_chars)
        if idempotency_key is not None and len(idempotency_key) > IDEMPOTENCY_KEY_WIDTH:
            raise invalid("Idempotency-Key", f"must be at most {IDEMPOTENCY_KEY_WIDTH} characters")
        return text

    def _title(self, text: str) -> str:
        return _SPACES.sub(" ", text).strip()[: self._settings.api.session_title_chars]

    @staticmethod
    def _system(document: Any, text: str, run_id: str, session_id: str) -> dict[str, Any]:
        """``system`` at turn start: the document's ``systemParams`` defaults, then the query and IDs."""
        system: dict[str, Any] = {}
        for param in getattr(document, "system_params", None) or []:
            name = param.name.removeprefix(_SYSTEM_PREFIX)
            default = getattr(param, "default_value", None)
            if default is not None:
                system[name] = default
        return {**system, "userQuery": text, "runId": run_id, "sessionId": session_id}

    async def _await_sync(self, run_id: str, session_id: str) -> RunResult | RunAccepted:
        outcome = await self._registry.wait(run_id, self._settings.runs.sync_wait_seconds)
        if outcome is None:
            return self._accepted(run_id, session_id, "running")
        return RunResult(
            run_id=run_id,
            session_id=session_id,
            status=_row_status(outcome),
            result=outcome.result,
            error=RunError(
                code=outcome.error.code, message=outcome.error.message, retryable=outcome.error.retryable
            )
            if outcome.error
            else None,
            usage=outcome.usage,
        )

    async def _replay(self, caller: Caller, run_id: str, session_id: str, mode: str) -> StartedRun:
        if mode == _STREAM:
            return StartedStream(
                run_id=run_id, session_id=session_id, events=await self.events(caller, run_id, None)
            )
        if self._registry.get(run_id) is not None:
            return await self._await_sync(run_id, session_id)
        row = await self.row(caller, run_id)
        if row.status in TERMINAL:
            return self._result(row)
        return self._accepted(run_id, session_id, row.status)

    @staticmethod
    def _accepted(run_id: str, session_id: str, status: RunStatus) -> RunAccepted:
        links = RunLinks.model_validate({"self": f"/v1/runs/{run_id}", "events": f"/v1/runs/{run_id}/events"})
        return RunAccepted(run_id=run_id, session_id=session_id, status=status, links=links)

    # -- CancelRun ----------------------------------------------------------------------------

    async def cancel(self, caller: Caller, run_id: str) -> Run:
        """``POST /v1/runs/{id}/cancel`` (design 9.5; cases H-12, E-17). A run this instance drives is
        stopped: its task is cancelled, and the turn is saved as ``cancelled`` within
        ``runs.cancelGraceSeconds``. A queued or waiting run, or one another instance drives, moves
        to ``cancelled`` with ``sp_run_transition``, which cancels its pending interrupts and deletes
        its checkpoint. A finished run is returned as it is."""
        row = await self.row(caller, run_id)  # another user's run: NOT_FOUND
        if row.status not in TERMINAL:
            if self._registry.cancel(run_id, _CANCELLED):
                await self._registry.wait(run_id, self._settings.runs.cancel_grace_seconds)
            else:
                error = self._catalog_error(_CANCELLED)
                patch = {"ownerSub": caller.subject, "error": {"code": error.code, "message": error.message}}
                await self._repository.call(
                    RunTransitionArgs(run_id=UUID(run_id), to="cancelled", patch=patch)
                )
        return self._run(await self.row(caller, run_id))

    # -- reads -----------------------------------------------------------------------------

    async def row(self, caller: Caller, run_id: str) -> RunRow:
        row = await self._repository.read_one(
            RunRow, owner=caller.subject, filters=[Filter(column="run_id", value=run_id)]
        )
        if row is None:
            raise ZonicError("NOT_FOUND", run_id=run_id)
        return row

    async def get(self, caller: Caller, run_id: str) -> tuple[Run, str]:
        """The run and a strong ETag over its state."""
        row = await self.row(caller, run_id)
        tag = hashlib.sha1(f"{row.run_id}|{row.status}|{row.updated_at.isoformat()}".encode()).hexdigest()
        return self._run(row), f'"{tag}"'

    async def nodes(self, caller: Caller, run_id: str) -> NodeVisitList:
        row = await self.row(caller, run_id)
        return NodeVisitList(items=[TimelineEntry.model_validate(e) for e in row.node_timeline])

    async def events(self, caller: Caller, run_id: str, last_event_id: int | None) -> Events:
        """Events after ``last_event_id`` while the run's buffer exists (``stream.gap`` for events no
        longer buffered); after the buffer is gone, a ``stream.gap`` and the final event rebuilt
        from the run row (case S-08)."""
        row = await self.row(caller, run_id)
        start = (last_event_id or 0) + 1
        stream = self._bus.get(run_id)
        if stream is not None:
            return stream.subscribe(start)
        # A run another instance drives: its events through that instance's internal route (Q-14).
        durable, address = self.durable, row.lease_address
        here = durable.instance.address if durable is not None else None
        if self.proxy is not None and row.status == "running" and address and address != here:
            return self.proxy.events(address, run_id, start - 1)
        return self._from_row(row, start)

    def local_events(self, run_id: str, last_event_id: int) -> Events | None:
        """The events of a run this instance drives, after ``last_event_id`` (the internal route)."""
        stream = self._bus.get(run_id)
        return stream.subscribe(last_event_id + 1) if stream is not None else None

    async def _from_row(self, row: RunRow, start: int) -> Events:
        run_id = str(row.run_id)
        now = self._clock.now()
        yield envelope(run_id, start, EventDraft(type="stream.gap", data={"fromSeq": start}), now)
        final = self._final(row)
        if final is not None:
            yield envelope(run_id, start + 1, final, now)

    def _final(self, row: RunRow) -> EventDraft | None:
        if row.status not in TERMINAL:
            return None
        if row.status == "succeeded":
            return EventDraft(type="run.completed", data={"result": row.result or {}, "usage": row.usage})
        if row.status == "cancelled":
            return EventDraft(type="run.cancelled", data={"reason": "cancelled"})
        error = self._error(row)
        return EventDraft(type="run.failed", data=error.model_dump(by_alias=True))

    # -- mapping -----------------------------------------------------------------------------

    def _catalog_error(self, code: str) -> RunError:
        entry = self._resources.errors.errors.get(code)
        if entry is None:
            code, entry = "INTERNAL", self._resources.errors.errors["INTERNAL"]
        return RunError(code=code, message=entry.message, retryable=entry.retryable)

    def _error(self, row: RunRow) -> RunError:
        saved = (row.error or {}).get("code")
        if isinstance(saved, str):
            return self._catalog_error(saved)
        return self._catalog_error(
            {"timed_out": "RUN_TIMEOUT", "cancelled": "CANCELLED"}.get(row.status, "INTERNAL")
        )

    def _run(self, row: RunRow) -> Run:
        failed = row.status in TERMINAL and row.status != "succeeded"
        return Run(
            run_id=str(row.run_id),
            session_id=str(row.session_id),
            agentic_workflow_id=row.agentic_workflow_id,
            revision=row.workflow_revision,
            status=row.status,
            mode=row.mode,
            result=row.result,
            error=self._error(row) if failed else None,
            usage=Usage.model_validate(row.usage) if row.usage else None,
            queued_at=row.queued_at,
            started_at=row.started_at,
            finished_at=row.finished_at,
            pending_interrupts=[str(i) for i in row.pending_interrupts],
        )

    def _result(self, row: RunRow) -> RunResult:
        failed = row.status in TERMINAL and row.status != "succeeded"
        return RunResult(
            run_id=str(row.run_id),
            session_id=str(row.session_id),
            status=row.status,
            result=row.result,
            error=self._error(row) if failed else None,
            usage=Usage.model_validate(row.usage) if row.usage else None,
        )


def _row_status(outcome: RunOutcome) -> RunStatus:
    return (
        "waiting_input"
        if outcome.status == "waiting_input"
        else "running"
        if outcome.status == "drained"
        else outcome.status
    )


__all__ = ["RunService", "RunStream", "StartedRun", "StartedStream"]
