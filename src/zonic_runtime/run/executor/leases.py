"""``RunLeases``: the leases of the runs this instance drives (design 9.5 step 3; plan P4-05; case
Q-13), after the Node.js runtime's ``run-leases.ts``.

One ``sp_run_heartbeat`` renews all of them every ``asyncRuns.leaseSeconds / 3``. A run the call
reports as no longer held (its lease expired and another instance claimed it) is lost: ``on_lost``
stops it here without saving, since the newer attempt would reject its writes anyway (fencing).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from typing import Any

from zonic_runtime.errors import ZonicError
from zonic_runtime.integrations.gateway.repository import GatewayRepository
from zonic_runtime.models.gateway.runtime import LOST_OUT, RunHeartbeatArgs
from zonic_runtime.models.resources.settings import AsyncRunSettings
from zonic_runtime.run.executor.instance import RunInstance

_BEATS_PER_LEASE = 3


def json_list(value: Any) -> list[Any]:
    """An INOUT ``jsonb`` array: the gateway returns it as JSON text or as the array."""
    found = json.loads(value) if isinstance(value, str) else value
    return list(found) if isinstance(found, list) else []


class RunLeases:
    def __init__(
        self,
        repository: GatewayRepository,
        instance: RunInstance,
        settings: AsyncRunSettings,
        on_lost: Callable[[str], None],
    ) -> None:
        self._repository = repository
        self._instance = instance
        self._settings = settings
        self._on_lost = on_lost
        self._held: set[str] = set()
        self._task: asyncio.Task[None] | None = None

    def hold(self, run_id: str) -> None:
        self._held.add(run_id)

    def drop(self, run_id: str) -> None:
        self._held.discard(run_id)

    def held(self) -> list[str]:
        return sorted(self._held)

    async def beat(self) -> list[str]:
        """One heartbeat: renews the held leases; the runs reported lost are stopped."""
        run_ids = self.held()
        if not run_ids:
            return []
        args = RunHeartbeatArgs(
            instance_id=self._instance.id, run_ids=run_ids, lease_seconds=self._settings.lease_seconds
        )
        lost = [str(r) for r in json_list((await self._repository.call(args)).get(LOST_OUT))]
        for run_id in lost:
            self._held.discard(run_id)
            self._on_lost(run_id)
        return lost

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._settings.lease_seconds / _BEATS_PER_LEASE)
            with contextlib.suppress(ZonicError):  # the gateway is down: the next beat retries
                await self.beat()

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
