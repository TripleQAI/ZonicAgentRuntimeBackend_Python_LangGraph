"""The interrupt use cases (design 9.4; plan P4-02, Section 15.14; cases H-01 to H-05, H-10).

* ``list``: the run's pending interrupts of its current pause, oldest first.
* ``answer`` (ResumeRun): the answer is checked against the interrupt's stored ``answerSchema``
  (``INTERRUPT_ANSWER_INVALID``), then by ``onResume`` hooks; it is recorded once, by the run's
  owner, before expiry (``sp_interrupt_answer``: ``INTERRUPT_ALREADY_ANSWERED``,
  ``INTERRUPT_EXPIRED``; another user's run is ``NOT_FOUND``); and the run resumes with it at
  once (``RunService.resume``). With two interrupts pending, one answer resumes its branch and the
  run pauses again on the other (the subset rule of SP-8, decision 0007).
* ``InterruptExpiry``: every ``interrupts.expiryCheckSeconds``, each instance handles the current
  pause's interrupts past ``expiresAt`` by ``interrupts.onExpiry``: ``cancel`` cancels the run
  (``sp_run_transition``, ``INTERRUPT_EXPIRED``); ``reject`` resumes an Approval as rejected
  (``{approved: false, expired: true}``) and cancels the run of any other interrupt. Attempt
  fencing makes a second instance's try fail harmlessly.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from zonic_runtime.application.caller import Caller
from zonic_runtime.application.runs import RunService, StartedRun
from zonic_runtime.config.settings import RuntimeResources
from zonic_runtime.errors import ZonicError
from zonic_runtime.integrations.gateway.repository import GatewayRepository
from zonic_runtime.io.schema_registry import SchemaRegistry
from zonic_runtime.models.api.requests import AnswerInterruptRequest
from zonic_runtime.models.api.responses import RunInterrupt, RunInterruptList
from zonic_runtime.models.gateway.runtime import (
    ExpiringInterruptRow,
    InterruptAnswerArgs,
    PendingInterruptRow,
    RunMode,
    RunRow,
    RunTransitionArgs,
)
from zonic_runtime.models.gateway.wire import Filter, FilterOp, OrderBy
from zonic_runtime.run.state.checkpoint_store import CheckpointStore
from zonic_runtime.runtime.clock import Clock, SystemClock

INVALID, NOT_FOUND, EXPIRED = "INTERRUPT_ANSWER_INVALID", "NOT_FOUND", "INTERRUPT_EXPIRED"
_YES, _NO = "yes", "no"
_REJECT = "reject"
#: Answer fields only the runtime writes (``ApprovalAnswer``).
_RUNTIME_FIELDS = ("decidedBy", "expired")
_SYNC: RunMode = "sync"


class InterruptService:
    def __init__(
        self,
        resources: RuntimeResources,
        runs: RunService,
        checkpoints: CheckpointStore,
        repository: GatewayRepository,
        schemas: SchemaRegistry,
        hooks: Any = None,
    ) -> None:
        self._resources = resources
        self._runs = runs
        self._checkpoints = checkpoints
        self._repository = repository
        self._schemas = schemas
        self._hooks = hooks

    def decides(self, form: str) -> bool:
        """An interrupt form that decides (Approval): its node type routes by ``yes`` and ``no``."""
        return any(
            entry.options.get("form") == form and _YES in entry.handles and _NO in entry.handles
            for entry in self._resources.manifest.node_types.values()
        )

    async def _pending(self, run: RunRow, owner: str) -> tuple[list[PendingInterruptRow], set[str]]:
        """The current pause's pending interrupts, and the IDs of earlier pauses' still pending."""
        rows = await self._repository.read(
            PendingInterruptRow,
            owner=owner,
            filters=[Filter(column="run_id", value=str(run.run_id))],
            order_by=[OrderBy(column="requested_at")],
        )
        current = [r for r in rows if r.attempt == run.attempt]
        return current, {str(r.interrupt_id) for r in rows if r.attempt != run.attempt}

    @staticmethod
    def _interrupt(row: PendingInterruptRow) -> RunInterrupt:
        return RunInterrupt(
            interrupt_id=str(row.interrupt_id),
            run_id=str(row.run_id),
            node_id=row.node_id,
            kind=row.kind,
            prompt=row.prompt,
            answer_schema=row.answer_schema,
            ui_schema=row.ui_schema,
            requested_at=row.requested_at,
            expires_at=row.expires_at,
        )

    async def list(self, caller: Caller, run_id: str) -> RunInterruptList:
        run = await self._runs.row(caller, run_id)
        current, _ = await self._pending(run, caller.subject)
        return RunInterruptList(items=[self._interrupt(r) for r in current])

    async def answer(
        self, caller: Caller, run_id: str, interrupt_id: str, request: AnswerInterruptRequest
    ) -> StartedRun:
        run = await self._runs.row(caller, run_id)  # another user's run: NOT_FOUND
        current, stale = await self._pending(run, caller.subject)
        target = next((r for r in current if str(r.interrupt_id) == interrupt_id), None)
        if target is None:
            if interrupt_id not in stale:  # answered, expired or unknown: the procedure says which
                with contextlib.suppress(ValueError):
                    args = InterruptAnswerArgs(
                        interrupt_id=UUID(interrupt_id), owner_sub=caller.subject, answer=request.answer
                    )
                    await self._repository.call(args)
            raise ZonicError(NOT_FOUND, interrupt_id=interrupt_id)
        issues = self._schemas.validate(target.answer_schema, request.answer)
        if issues:
            raise ZonicError(INVALID, issues=[{"path": i.path, "message": i.message} for i in issues])
        answer = request.answer
        if isinstance(answer, Mapping):
            answer = {k: v for k, v in answer.items() if k not in _RUNTIME_FIELDS}
            if self.decides(target.kind):
                answer["decidedBy"] = caller.subject
        checkpoint = await self._checkpoints.load(run_id, caller.subject)
        if checkpoint is None:
            raise ZonicError(NOT_FOUND, run_id=run_id)
        if self._hooks is not None:
            blueprint = self._runs.prepared(checkpoint).blueprint
            await self._hooks.on_resume(blueprint, run_id, interrupt_id, answer)
        await self._repository.call(
            InterruptAnswerArgs(interrupt_id=target.interrupt_id, owner_sub=caller.subject, answer=answer)
        )
        return await self._runs.resume(caller, checkpoint, {interrupt_id: answer}, request.mode)


class InterruptExpiry:
    def __init__(
        self,
        resources: RuntimeResources,
        runs: RunService,
        checkpoints: CheckpointStore,
        repository: GatewayRepository,
        interrupts: InterruptService,
        clock: Clock | None = None,
    ) -> None:
        self._settings = resources.settings.interrupts
        self._runs = runs
        self._checkpoints = checkpoints
        self._repository = repository
        self._interrupts = interrupts
        self._clock = clock or SystemClock()
        self._task: asyncio.Task[None] | None = None
        #: The last pass: interrupts handled, and the codes of the ones skipped.
        self.last: dict[str, Any] | None = None

    async def run(self) -> int:
        """One pass: the interrupts past their expiry it handled."""
        now = self._clock.now()
        rows = await self._repository.read(
            ExpiringInterruptRow, filters=[Filter(column="expires_at", op=FilterOp.LT, value=now.isoformat())]
        )
        handled, skipped = 0, []
        for row in rows:
            caller = Caller(subject=row.owner_sub)
            try:
                run = await self._runs.row(caller, str(row.run_id))
                if run.attempt != row.attempt or run.status != "waiting_input":
                    continue  # an earlier pause's interrupt, or a run that moved on
                if self._settings.on_expiry == _REJECT and self._interrupts.decides(row.kind):
                    checkpoint = await self._checkpoints.load(str(row.run_id), row.owner_sub)
                    if checkpoint is None:
                        continue
                    answer = {"approved": False, "expired": True}
                    await self._runs.resume(
                        caller,
                        checkpoint,
                        {str(row.interrupt_id): answer},
                        _SYNC,
                        tenant=run.tenant_id,
                        wait=False,
                    )
                else:
                    await self._repository.call(
                        RunTransitionArgs(
                            run_id=row.run_id,
                            to="cancelled",
                            patch={
                                "ownerSub": row.owner_sub,
                                "attempt": row.attempt,
                                "error": {"code": EXPIRED},
                            },
                        )
                    )
                handled += 1
            except ZonicError as error:  # another instance handled it, or the run moved on
                skipped.append(error.code)
        self.last = {"handled": handled, "skipped": skipped}
        return handled

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._settings.expiry_check_seconds)
            try:
                await self.run()
            except ZonicError as error:
                self.last = {"handled": 0, "failed": error.code}

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
