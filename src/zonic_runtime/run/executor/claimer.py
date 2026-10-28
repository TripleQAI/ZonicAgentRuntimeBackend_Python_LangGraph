"""``RunClaimer``: ``async`` runs from the durable queue (design 9.5 step 2; plan P4-05; cases Q-10,
Q-11), after the Node.js runtime's ``run-claimer.ts``.

While the instance has free permits, ``sp_run_claim`` takes up to ``asyncRuns.claimBatch`` runs:
queued ones (round-robin across tenants, then priority) and running ones whose lease expired (their
instance stopped). It claims every ``claimIntervalMilliseconds`` while runs come, and backs off to
``idleClaimIntervalMilliseconds`` while the queue is empty. Each claim is handed to ``on_claimed``.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from zonic_runtime.errors import ZonicError
from zonic_runtime.integrations.gateway.repository import GatewayRepository
from zonic_runtime.models.gateway.runtime import RUNS_OUT, RunClaimArgs
from zonic_runtime.models.resources.settings import AsyncRunSettings
from zonic_runtime.run.executor.executor import RunExecutor
from zonic_runtime.run.executor.instance import RunInstance
from zonic_runtime.run.executor.leases import json_list

_MODES = frozenset({"stream", "sync", "async"})


@dataclass(frozen=True)
class ClaimedRun:
    run_id: str
    #: The claim's fencing token (design 9.5 step 3): every later write of the run carries it.
    attempt: int
    mode: str
    session_id: str
    #: ``queued``, or ``running`` when the run's instance stopped (its lease expired).
    previous_status: str

    @classmethod
    def read(cls, value: Any) -> ClaimedRun | None:
        if not isinstance(value, Mapping) or value.get("mode") not in _MODES:
            return None
        return cls(
            run_id=str(value.get("runId") or ""),
            attempt=int(value.get("attempt") or 0),
            mode=str(value["mode"]),
            session_id=str(value.get("sessionId") or ""),
            previous_status=str(value.get("previousStatus") or ""),
        )


class RunClaimer:
    def __init__(
        self,
        repository: GatewayRepository,
        instance: RunInstance,
        settings: AsyncRunSettings,
        executor: RunExecutor,
        on_claimed: Callable[[ClaimedRun], Awaitable[None]],
        capacity: int,
    ) -> None:
        self._repository = repository
        self._instance = instance
        self._settings = settings
        self._executor = executor
        self._on_claimed = on_claimed
        self._capacity = capacity
        self._task: asyncio.Task[None] | None = None

    async def claim(self) -> list[ClaimedRun]:
        """One claim cycle: the runs claimed (none without a free permit)."""
        free = self._capacity - self._executor.active
        if free <= 0:
            return []
        args = RunClaimArgs(
            instance=self._instance.wire(),
            limit=min(free, self._settings.claim_batch),
            lease_seconds=self._settings.lease_seconds,
        )
        found = json_list((await self._repository.call(args)).get(RUNS_OUT))
        claimed = [c for c in (ClaimedRun.read(v) for v in found) if c is not None and c.run_id]
        for run in claimed:
            with contextlib.suppress(ZonicError):  # its failure is recorded on the run
                await self._on_claimed(run)
        return claimed

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            try:
                claimed = await self.claim()
            except ZonicError:
                claimed = []
            interval = (
                self._settings.claim_interval_milliseconds
                if claimed
                else self._settings.idle_claim_interval_milliseconds
            )
            await asyncio.sleep(interval / 1000)

    async def stop(self) -> None:
        """No new claims (a graceful drain)."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
