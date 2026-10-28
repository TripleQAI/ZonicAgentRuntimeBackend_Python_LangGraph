"""Rate limits per user and per tenant: token buckets refilled per minute (case A-12).

A request over either limit is ``RATE_LIMITED`` (429) with ``Retry-After``: the seconds until a
token is back. Buckets are per process and bounded by an LRU (case R-06).
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from zonic_runtime.errors import ZonicError
from zonic_runtime.models.resources.settings import RateLimitSettings
from zonic_runtime.runtime.lru import LruCache

_MAX_BUCKETS = 50_000


@dataclass
class _Bucket:
    tokens: float
    at: float


class RateLimiter:
    def __init__(self, settings: RateLimitSettings, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._limits = {"user": settings.per_user_per_minute, "tenant": settings.per_tenant_per_minute}
        self._monotonic = monotonic
        self._buckets: LruCache[tuple[str, str], _Bucket] = LruCache(_MAX_BUCKETS)

    def _take(self, scope: str, key: str) -> float:
        """Takes a token; returns 0, or the seconds until one is available."""
        per_minute = self._limits[scope]
        rate = per_minute / 60.0
        now = self._monotonic()
        bucket = self._buckets.get((scope, key))
        if bucket is None:
            bucket = _Bucket(tokens=float(per_minute), at=now)
            self._buckets.put((scope, key), bucket)
        bucket.tokens = min(float(per_minute), bucket.tokens + (now - bucket.at) * rate)
        bucket.at = now
        if bucket.tokens < 1.0:
            return (1.0 - bucket.tokens) / rate
        bucket.tokens -= 1.0
        return 0.0

    def check(self, user: str, tenant: str | None) -> None:
        wait = self._take("user", user)
        if not wait and tenant:
            wait = self._take("tenant", tenant)
        if wait:
            raise ZonicError("RATE_LIMITED", retry_after=max(1, math.ceil(wait)))
