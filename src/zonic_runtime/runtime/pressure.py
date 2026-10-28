"""Process pressure (design 11.3, "Back-pressure, load shedding"; plan P2-05).

``PressureMonitor`` samples the event loop's lag (how late a timer fires) every
``pressure.sampleIntervalMilliseconds`` and the process's resident memory. Above
``pressure.maxEventLoopLagMilliseconds`` or ``maxRssMegabytes`` the process is overloaded: new
runs get ``503 OVERLOADED`` and readiness reports ``degraded``; running runs continue.

The lag that counts is the 90th percentile of the last ``_RECENT`` samples, not the last sample
alone: one garbage-collection pause or one burst of request handling is a single slow sample, and
shedding new runs for it would be noise. Sustained lag still trips it within a few samples.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

from zonic_runtime.models.resources.settings import PressureSettings

_SAMPLES = 120
_RECENT = 10
_RECENT_PERCENTILE = 0.9
_MEGABYTE = 1024 * 1024
_STATM = "/proc/self/statm"


def rss_megabytes() -> float:
    """Resident memory now (Linux, from ``/proc``), the peak elsewhere on Unix, 0 on Windows."""
    if sys.platform == "win32":
        return 0.0
    statm = Path(_STATM)
    if statm.exists():
        resident_pages = int(statm.read_text().split()[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE") / _MEGABYTE
    import resource

    peak = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return peak / _MEGABYTE if sys.platform == "darwin" else peak / 1024


class PressureMonitor:
    def __init__(
        self,
        settings: PressureSettings,
        *,
        rss: Callable[[], float] = rss_megabytes,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._rss = rss
        self._monotonic = monotonic
        self._lags: deque[float] = deque(maxlen=_SAMPLES)
        self._task: asyncio.Task[None] | None = None

    def rss_megabytes(self) -> float:
        """Resident memory now, as the RSS threshold reads it."""
        return self._rss()

    @property
    def lag_milliseconds(self) -> float:
        return self._lags[-1] if self._lags else 0.0

    def percentile(self, fraction: float) -> float:
        if not self._lags:
            return 0.0
        ordered = sorted(self._lags)
        return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]

    @property
    def recent_lag_milliseconds(self) -> float:
        """The 90th percentile of the last ``_RECENT`` samples: what ``overloaded`` compares."""
        recent = sorted(list(self._lags)[-_RECENT:])
        return recent[int(_RECENT_PERCENTILE * (len(recent) - 1))] if recent else 0.0

    def overloaded(self) -> bool:
        return (
            self.recent_lag_milliseconds > self._settings.max_event_loop_lag_milliseconds
            or self._rss() > self._settings.max_rss_megabytes
        )

    async def _sample(self) -> None:
        interval = self._settings.sample_interval_milliseconds / 1000
        while True:
            started = self._monotonic()
            await asyncio.sleep(interval)
            self._lags.append(max(0.0, (self._monotonic() - started - interval) * 1000))

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._sample())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
