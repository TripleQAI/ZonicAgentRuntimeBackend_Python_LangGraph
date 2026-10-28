"""The run event bus and per-run replay buffers (design Section 10; plan P2-01, DR-9).

``RunStream`` is one run's live stream on this instance. It adds the envelope (``seq``
strictly increasing with no gaps, continuing from the run's ``last_event_seq`` after a
resume), caps the data size, keeps the last ``stream.replayBufferEvents`` events, and lets any
number of subscribers read from a sequence number. The run never waits for a subscriber: one
that falls behind the buffer receives ``stream.gap`` and continues from the oldest buffered
event. Events are never persisted.

With ``coalescing`` (``sse.coalesceMilliseconds``, ``sse.coalesceBytes``; case Q-04), text deltas
are joined before they get a ``seq``: a node's first delta goes out at once, later ones are joined
until the interval passes, the bytes collect, or another event is published. Joining here, not
per subscriber, keeps ``seq`` without gaps and every subscriber's view the same.

``RunEventBus`` holds the streams of this instance and releases each one
``stream.replayRetentionSeconds`` after it closed, so memory stays bounded (case R-06).
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from zonic_runtime.models.events import EventDraft, RunEventType
from zonic_runtime.models.resources.settings import StreamSettings
from zonic_runtime.run.events.summarizer import cap_data
from zonic_runtime.runtime.clock import Clock, SystemClock

GAP: RunEventType = "stream.gap"
_DELTAS = frozenset({"token.delta", "reasoning.delta"})


def _timestamp(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def envelope(
    run_id: str, seq: int, draft: EventDraft, ts: datetime, truncated: bool = False
) -> dict[str, Any]:
    event: dict[str, Any] = {"id": f"{run_id}:{seq}", "runId": run_id, "seq": seq, "type": draft.type}
    event["ts"] = _timestamp(ts)
    if draft.node_id is not None:
        event["nodeId"] = draft.node_id
    if draft.path is not None:
        event["path"] = draft.path
    if truncated:
        event["truncated"] = True
    event["data"] = draft.data
    return event


@dataclass(frozen=True)
class Coalescing:
    milliseconds: int
    bytes: int


@dataclass
class _Pending:
    key: tuple[str, str | None, str | None]
    draft: EventDraft
    text: str
    timer: asyncio.TimerHandle


class RunStream:
    def __init__(
        self,
        run_id: str,
        settings: StreamSettings,
        max_event_bytes: int,
        *,
        start_seq: int = 0,
        clock: Clock | None = None,
        coalescing: Coalescing | None = None,
    ) -> None:
        self.run_id = run_id
        self._settings = settings
        self._max_event_bytes = max_event_bytes
        self._clock = clock or SystemClock()
        self._buffer: deque[dict[str, Any]] = deque(maxlen=settings.replay_buffer_events)
        self._seq = start_seq
        self._closed_at: float | None = None
        self._changed = asyncio.Event()
        self._coalescing = coalescing
        self._pending: _Pending | None = None
        self._started: set[tuple[str, str | None, str | None]] = set()

    def publish(self, draft: EventDraft) -> dict[str, Any] | None:
        """The published event, or ``None`` for a text delta held to be joined."""
        if self._closed_at is not None:
            raise RuntimeError(f"run stream {self.run_id} is closed")
        if self._coalescing is not None and draft.type in _DELTAS:
            return self._delta(draft, self._coalescing)
        self.flush()
        return self._emit(draft)

    def _delta(self, draft: EventDraft, coalescing: Coalescing) -> dict[str, Any] | None:
        key = (draft.type, draft.path, draft.node_id)
        text = str(draft.data.get("text", ""))
        pending = self._pending
        if pending is not None and pending.key == key:
            pending.text += text
            if len(pending.text.encode("utf-8")) >= coalescing.bytes:
                self.flush()
            return None
        self.flush()
        if key not in self._started:
            self._started.add(key)
            return self._emit(draft)
        timer = asyncio.get_running_loop().call_later(coalescing.milliseconds / 1000, self.flush)
        self._pending = _Pending(key=key, draft=draft, text=text, timer=timer)
        return None

    def flush(self) -> None:
        """Publishes the held text delta, if any."""
        pending, self._pending = self._pending, None
        if pending is None:
            return
        pending.timer.cancel()
        draft = pending.draft
        self._emit(
            EventDraft(
                type=draft.type,
                data={**draft.data, "text": pending.text},
                node_id=draft.node_id,
                path=draft.path,
            )
        )

    def _emit(self, draft: EventDraft) -> dict[str, Any]:
        self._seq += 1
        data, truncated = cap_data(draft.data, self._max_event_bytes)
        if truncated:
            draft = EventDraft(type=draft.type, data=data, node_id=draft.node_id, path=draft.path)
        event = envelope(self.run_id, self._seq, draft, self._clock.now(), truncated)
        self._buffer.append(event)
        self._wake()
        return event

    def close(self) -> None:
        if self._closed_at is None:
            self.flush()
            self._closed_at = self._clock.monotonic()
            self._wake()

    def _wake(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()

    @property
    def closed(self) -> bool:
        return self._closed_at is not None

    @property
    def last_seq(self) -> int:
        return self._seq

    @property
    def oldest_seq(self) -> int:
        """The oldest buffered sequence number; a subscriber asking for less gets ``stream.gap``."""
        return self._buffer[0]["seq"] if self._buffer else self._seq + 1

    @property
    def size(self) -> int:
        return len(self._buffer)

    def expired(self) -> bool:
        return (
            self._closed_at is not None
            and self._clock.monotonic() - self._closed_at >= self._settings.replay_retention_seconds
        )

    def gap(self, from_seq: int, to_seq: int) -> dict[str, Any]:
        """A ``stream.gap`` event, never buffered; its ``seq`` is the last missing event's, so a
        client resuming from it continues with the oldest buffered one."""
        draft = EventDraft(type=GAP, data={"fromSeq": from_seq, "toSeq": to_seq})
        return envelope(self.run_id, to_seq, draft, self._clock.now())

    async def subscribe(self, from_seq: int = 1) -> AsyncIterator[dict[str, Any]]:
        """Events with ``seq >= from_seq``: the buffered ones, then live ones until the stream
        closes. The iterator never blocks the publisher."""
        next_seq = max(1, from_seq)
        while True:
            changed = self._changed
            oldest = self.oldest_seq
            if next_seq < oldest:
                yield self.gap(next_seq, oldest - 1)
                next_seq = oldest
            for event in list(self._buffer):  # a snapshot: publishing trims the buffer
                if event["seq"] >= next_seq:
                    next_seq = event["seq"] + 1
                    yield event
            if self._seq >= next_seq:
                continue  # published while this subscriber was yielding
            if self._closed_at is not None:
                return
            await changed.wait()


class RunEventBus:
    def __init__(
        self,
        settings: StreamSettings,
        max_event_bytes: int,
        clock: Clock | None = None,
        coalescing: Coalescing | None = None,
    ) -> None:
        self._settings = settings
        self._max_event_bytes = max_event_bytes
        self._clock = clock or SystemClock()
        self._coalescing = coalescing
        self._streams: dict[str, RunStream] = {}

    def create(self, run_id: str, start_seq: int = 0) -> RunStream:
        """A new stream for a run's turn; ``start_seq`` is its ``last_event_seq`` (DR-9)."""
        self.sweep()
        stream = RunStream(
            run_id,
            self._settings,
            self._max_event_bytes,
            start_seq=start_seq,
            clock=self._clock,
            coalescing=self._coalescing,
        )
        self._streams[run_id] = stream
        return stream

    def get(self, run_id: str) -> RunStream | None:
        self.sweep()
        return self._streams.get(run_id)

    def sweep(self) -> None:
        for run_id in [r for r, s in self._streams.items() if s.expired()]:
            del self._streams[run_id]

    @property
    def size(self) -> int:
        return len(self._streams)

    @property
    def active(self) -> int:
        return sum(1 for s in self._streams.values() if not s.closed)
