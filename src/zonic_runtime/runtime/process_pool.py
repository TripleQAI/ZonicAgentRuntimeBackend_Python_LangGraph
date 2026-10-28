"""``CpuPool``: pure-Python CPU work off the event loop (design 11.2, "CPU work"; case Q-16).

Masking, truncation and validation of a payload of ``cpu.offloadBytes`` or more run in a
``ProcessPoolExecutor`` of ``cpu.workers`` (threads would not help: the GIL); smaller payloads run
inline, where a process round trip would cost more than the work. The pool starts on first use.
Functions and arguments must pickle: module-level functions and plain data.
"""

from __future__ import annotations

import asyncio
import multiprocessing
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from typing import Any, TypeVar

from zonic_runtime.models.resources.settings import CpuSettings

T = TypeVar("T")

_START_METHOD = "spawn"  # no fork of a process with threads and an event loop


class CpuPool:
    def __init__(self, settings: CpuSettings) -> None:
        self._settings = settings
        self._pool: ProcessPoolExecutor | None = None
        #: Calls sent to the pool, for tests and ``/metrics``.
        self.offloaded = 0

    def offloads(self, size: int) -> bool:
        return size >= self._settings.offload_bytes

    async def run(self, size: int, function: Callable[..., T], *arguments: Any) -> T:
        """``function(*arguments)``: in the pool when ``size`` (the payload's bytes) is at least
        ``cpu.offloadBytes``, else inline."""
        if not self.offloads(size):
            return function(*arguments)
        if self._pool is None:
            self._pool = ProcessPoolExecutor(
                max_workers=self._settings.workers, mp_context=multiprocessing.get_context(_START_METHOD)
            )
        self.offloaded += 1
        return await asyncio.get_running_loop().run_in_executor(self._pool, function, *arguments)

    def shutdown(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
