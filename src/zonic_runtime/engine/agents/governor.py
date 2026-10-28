"""Model governors: process-wide limits per model (design 11.2; plan P2-05, cases Q-08, R-02).

Each catalog model has one ``ModelGovernor``: at most ``quota.maxInFlight`` calls at once, and
token buckets for ``requestsPerMinute`` and ``tokensPerMinute``. A call waits for a permit until
``quota.maxQueueWaitMilliseconds``; then it fails with ``MODEL_BUSY``, which the LLM and agent
retry policies retry with backoff, so a burst never makes a run hang. Governors are created on
first use and shared by every run of the process.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from zonic_runtime.errors import ZonicError
from zonic_runtime.models.resources.catalogs import ModelQuota

BUSY = "MODEL_BUSY"
_MINUTE = 60.0


class TokenBucket:
    """``per_minute`` tokens, refilled continuously; ``wait(n)`` is the time until ``n`` are there."""

    def __init__(self, per_minute: float, monotonic: Callable[[], float]) -> None:
        self._capacity = float(per_minute)
        self._rate = per_minute / _MINUTE
        self._tokens = float(per_minute)
        self._at = monotonic()
        self._monotonic = monotonic

    def _refill(self) -> None:
        now = self._monotonic()
        self._tokens = min(self._capacity, self._tokens + (now - self._at) * self._rate)
        self._at = now

    def wait(self, amount: float) -> float:
        self._refill()
        needed = min(amount, self._capacity)
        return 0.0 if self._tokens >= needed else (needed - self._tokens) / self._rate

    def take(self, amount: float) -> None:
        self._refill()
        self._tokens -= min(amount, self._capacity)


class ModelGovernor:
    def __init__(
        self, model_id: str, quota: ModelQuota, monotonic: Callable[[], float] = time.monotonic
    ) -> None:
        self.model_id = model_id
        self.quota = quota
        self._monotonic = monotonic
        self._slots = asyncio.Semaphore(quota.max_in_flight)
        self._requests = TokenBucket(quota.requests_per_minute, monotonic)
        self._tokens = TokenBucket(quota.tokens_per_minute, monotonic)
        self.in_flight = 0
        #: Calls waiting for a permit (``/metrics``).
        self.queued = 0
        #: The most calls in flight at once, for tests (case Q-08).
        self.peak = 0

    @asynccontextmanager
    async def permit(self, tokens: int = 0) -> AsyncIterator[None]:
        deadline = self._monotonic() + self.quota.max_queue_wait_milliseconds / 1000
        self.queued += 1
        try:
            try:
                async with asyncio.timeout(max(0.0, deadline - self._monotonic())):
                    await self._slots.acquire()
            except TimeoutError as exc:
                raise ZonicError(BUSY, model=self.model_id, reason="in flight") from exc
            granted = False
            try:
                while (wait := max(self._requests.wait(1), self._tokens.wait(tokens))) > 0:
                    if self._monotonic() + wait > deadline:
                        raise ZonicError(BUSY, model=self.model_id, reason="rate")
                    await asyncio.sleep(wait)
                granted = True
            finally:
                if not granted:  # a refused or cancelled wait gives the slot back
                    self._slots.release()
        finally:
            self.queued -= 1
        self._requests.take(1)
        self._tokens.take(tokens)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            yield
        finally:
            self.in_flight -= 1
            self._slots.release()


class ModelGovernors:
    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._governors: dict[str, ModelGovernor] = {}

    def get(self, model_id: str, quota: ModelQuota) -> ModelGovernor:
        found = self._governors.get(model_id)
        if found is None:
            found = self._governors[model_id] = ModelGovernor(model_id, quota, self._monotonic)
        return found

    def all(self) -> list[ModelGovernor]:
        return list(self._governors.values())
