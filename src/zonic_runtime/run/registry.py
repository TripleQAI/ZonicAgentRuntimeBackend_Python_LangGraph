"""``RunRegistry``: the runs this instance drives (plan P2-03; P2-05 adds admission).

A run's task is independent of the request that started it: a client that disconnects does not
cancel it (``runs.cancelOnDisconnect`` is false, case S-07). ``sync`` requests and idempotent
replays wait on the task here.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from zonic_runtime.run.driver import ActiveRun, RunOutcome


@dataclass
class _Entry:
    run: ActiveRun
    task: asyncio.Task[RunOutcome]


class RunRegistry:
    def __init__(self) -> None:
        self._runs: dict[str, _Entry] = {}

    def add(self, run: ActiveRun, task: asyncio.Task[RunOutcome]) -> None:
        self._runs[run.run_id] = _Entry(run, task)
        task.add_done_callback(lambda _: self._runs.pop(run.run_id, None))

    def get(self, run_id: str) -> ActiveRun | None:
        entry = self._runs.get(run_id)
        return entry.run if entry is not None else None

    def task(self, run_id: str) -> asyncio.Task[RunOutcome] | None:
        entry = self._runs.get(run_id)
        return entry.task if entry is not None else None

    async def wait(self, run_id: str, seconds: float) -> RunOutcome | None:
        """The run's outcome, or ``None`` when it is still running after ``seconds``."""
        task = self.task(run_id)
        if task is None:
            return None
        done, _ = await asyncio.wait({task}, timeout=seconds)
        return task.result() if done else None

    def cancel(self, run_id: str, reason: str) -> bool:
        entry = self._runs.get(run_id)
        if entry is None:
            return False
        entry.run.cancel(reason)
        return True

    def runs(self) -> list[ActiveRun]:
        return [e.run for e in self._runs.values()]

    @property
    def active(self) -> int:
        return len(self._runs)

    async def settle(self, seconds: float) -> None:
        """Waits until the runs active here end, at most ``seconds`` (a graceful drain)."""
        tasks = [e.task for e in self._runs.values()]
        if tasks:
            await asyncio.wait(tasks, timeout=seconds)

    async def drain(self) -> None:
        """Waits for every run of this instance (shutdown; P4-05 adds the drain to checkpoints)."""
        tasks = [e.task for e in self._runs.values()]
        if tasks:
            await asyncio.wait(tasks)
