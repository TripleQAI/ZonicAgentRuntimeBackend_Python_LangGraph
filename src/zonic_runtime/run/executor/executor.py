"""``RunExecutor``: admission of runs to this process (design 11.2; plan P2-05, case R-04).

At most ``runs.maxConcurrentRuns`` runs at once per process; one more is ``RATE_LIMITED``
(``429``) with ``Retry-After``, and the runs already running are not affected. An overloaded
process (``PressureMonitor``) refuses new runs with ``OVERLOADED`` (``503``). The caps per session,
user and tenant are enforced by ``sp_run_start`` across every instance.
"""

from __future__ import annotations

from collections.abc import Callable

from zonic_runtime.errors import ZonicError
from zonic_runtime.models.resources.settings import RunsSettings
from zonic_runtime.runtime.pressure import PressureMonitor

_RETRY_AFTER = 1


class RunExecutor:
    def __init__(self, settings: RunsSettings, pressure: PressureMonitor | None = None) -> None:
        self._settings = settings
        self._pressure = pressure
        self.active = 0

    def admit(self) -> Callable[[], None]:
        """A permit for one run, or ``RATE_LIMITED``/``OVERLOADED``; call the result once to release it."""
        if self._pressure is not None and self._pressure.overloaded():
            raise ZonicError("OVERLOADED", retry_after=_RETRY_AFTER)
        if self.active >= self._settings.max_concurrent_runs:
            raise ZonicError("RATE_LIMITED", retry_after=_RETRY_AFTER, limit="maxConcurrentRuns")
        self.active += 1
        released = False

        def release() -> None:
            nonlocal released
            if not released:
                released = True
                self.active -= 1

        return release
