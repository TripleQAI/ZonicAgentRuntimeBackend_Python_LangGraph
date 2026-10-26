"""``FakeClock``: wall and monotonic time that move only when a test says so (P0-11).

``now()`` is a timezone-aware UTC ``datetime``; ``monotonic()`` is seconds as a float.
``sleep`` advances the clock instead of waiting and records the duration, so code that
takes a clock (leases, heartbeats, expiry, retry backoff) runs instantly in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass
class FakeClock:
    start: datetime = EPOCH
    _elapsed: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.start.tzinfo is None:
            raise ValueError("FakeClock.start must be timezone-aware")

    def now(self) -> datetime:
        return self.start + timedelta(seconds=self._elapsed)

    def monotonic(self) -> float:
        return self._elapsed

    def advance(self, seconds: float) -> datetime:
        if seconds < 0:
            raise ValueError("a clock cannot go backwards")
        self._elapsed += seconds
        return self.now()

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(max(seconds, 0.0))
