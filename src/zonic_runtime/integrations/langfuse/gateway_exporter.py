"""``GatewaySpanExporter``: finished spans to ``sp_obs_ingest`` (design 12; plan P2-08, cases O-05,
O-06, L-11).

It runs on the span processor's worker thread, never on the event loop, so a run never waits for
tracing and never fails because of it.

* Spans become records (``LangfuseRecordMapper``) and join a bounded queue of
  ``export.maxQueueSize`` records. When the queue is full the oldest records are dropped and
  counted (``dropped``, ``/metrics``).
* The queue goes out in batches of at most ``export.maxObservationsPerCall`` records and
  ``export.maxBytesPerCall`` bytes (O-05), oldest first.
* ``sp_obs_ingest`` is idempotent, so a batch the gateway could not take (``DATA_UNAVAILABLE``)
  is sent again: ``export.retryAttempts`` times, with backoff from ``export.retryBackoffMilliseconds``.
  After that the batch stays queued for the next export, and readiness reports tracing
  ``degraded`` (L-11). A batch the gateway refuses for any other reason would be refused again,
  so it is dropped and counted.
* ``force_flush`` and ``shutdown`` send what is queued, within their deadline
  (``export.shutdownFlushTimeoutMilliseconds``).

``GatewayIngest`` reaches the async gateway from this thread through the app's event loop
(``bind`` at start-up). Code on the loop thread must flush or shut the tracer down through
``asyncio.to_thread``: from the loop thread itself the call could never complete.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from zonic_runtime.errors import ZonicError
from zonic_runtime.integrations.gateway.repository import GatewayRepository
from zonic_runtime.integrations.langfuse.record_mapper import LangfuseRecordMapper
from zonic_runtime.models.observability import ObsIngestArgs
from zonic_runtime.models.resources.settings import ExportSettings

#: Sends one batch; raises ``ZonicError`` when the gateway did not take it.
Ingest = Callable[[ObsIngestArgs], None]
UNAVAILABLE = "DATA_UNAVAILABLE"
_Kind = Literal["traces", "observations", "scores"]


@dataclass(frozen=True)
class _Record:
    kind: _Kind
    data: dict[str, Any]
    size: int


class GatewayIngest:
    def __init__(self, repository: GatewayRepository, timeout_seconds: float) -> None:
        self._repository = repository
        self._timeout = timeout_seconds
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def __call__(self, args: ObsIngestArgs) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            raise ZonicError(UNAVAILABLE, reason="no event loop")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            raise RuntimeError("the span exporter was called on the event loop thread; use asyncio.to_thread")
        future = asyncio.run_coroutine_threadsafe(self._repository.call(args), loop)
        try:
            future.result(self._timeout)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise ZonicError(UNAVAILABLE, reason="timeout") from exc
        except concurrent.futures.CancelledError as exc:  # the loop stopped under it
            raise ZonicError(UNAVAILABLE, reason="cancelled") from exc


class GatewaySpanExporter(SpanExporter):
    def __init__(
        self,
        ingest: Ingest,
        mapper: LangfuseRecordMapper,
        settings: ExportSettings,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ingest = ingest
        self._mapper = mapper
        self._settings = settings
        self._monotonic = monotonic
        self._queue: deque[_Record] = deque()
        self._lock = threading.Lock()
        self._stopped = threading.Event()  # also interrupts a backoff wait at shutdown
        #: Counters for ``/metrics`` and readiness.
        self.dropped = 0
        self.sent = 0
        self.calls = 0
        self.failed_calls = 0
        self.healthy = True
        self.last_error: str | None = None

    @property
    def queued(self) -> int:
        return len(self._queue)

    # -- SpanExporter -------------------------------------------------------------------------

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        traces, observations = self._mapper.records(spans)
        with self._lock:
            self._enqueue([*self._wrap("traces", traces), *self._wrap("observations", observations)])
            ok = self._drain(deadline=None)
        return SpanExportResult.SUCCESS if ok else SpanExportResult.FAILURE

    def score(self, record: dict[str, Any]) -> None:
        """An ``ObsScoreRecord`` (the ``annotate`` hook); it goes out with the next batch."""
        with self._lock:
            self._enqueue(self._wrap("scores", [record]))

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        with self._lock:
            return self._drain(deadline=self._monotonic() + timeout_millis / 1000)

    def shutdown(self) -> None:
        self.force_flush(self._settings.shutdown_flush_timeout_milliseconds)
        self._stopped.set()

    # -- queue ----------------------------------------------------------------------------------

    @staticmethod
    def _wrap(kind: _Kind, records: list[dict[str, Any]]) -> list[_Record]:
        return [
            _Record(kind, r, len(json.dumps(r, ensure_ascii=False, default=str).encode("utf-8")))
            for r in records
        ]

    def _enqueue(self, records: list[_Record]) -> None:
        self._queue.extend(records)
        while len(self._queue) > self._settings.max_queue_size:  # O-06: the oldest go first
            self._queue.popleft()
            self.dropped += 1

    def _batch(self) -> list[_Record]:
        """The oldest records that fit one call; a record alone above the byte cap goes alone."""
        batch: list[_Record] = []
        size = 0
        for record in self._queue:
            if batch and (
                len(batch) >= self._settings.max_observations_per_call
                or size + record.size > self._settings.max_bytes_per_call
            ):
                break
            batch.append(record)
            size += record.size
        return batch

    def _drain(self, deadline: float | None) -> bool:
        while self._queue:
            batch = self._batch()
            outcome = self._send(batch, deadline)
            if outcome == "kept":
                return False
            for _ in batch:
                self._queue.popleft()
            if outcome == "refused":
                self.dropped += len(batch)
        return True

    def _send(self, batch: list[_Record], deadline: float | None) -> Literal["sent", "refused", "kept"]:
        args = ObsIngestArgs(
            traces=[r.data for r in batch if r.kind == "traces"],
            observations=[r.data for r in batch if r.kind == "observations"],
            scores=[r.data for r in batch if r.kind == "scores"],
        )
        attempts = self._settings.retry_attempts + 1
        for attempt in range(attempts):
            self.calls += 1
            try:
                self._ingest(args)
            except ZonicError as error:
                self.failed_calls += 1
                self.last_error = error.code
                if error.code != UNAVAILABLE:
                    return "refused"
            else:
                self.sent += len(batch)
                self.healthy = True
                return "sent"
            wait = self._settings.retry_backoff_milliseconds / 1000 * (2**attempt)
            if attempt + 1 == attempts or self._past(deadline, wait) or self._stopped.wait(wait):
                break
        self.healthy = False
        return "kept"

    def _past(self, deadline: float | None, wait: float) -> bool:
        return deadline is not None and self._monotonic() + wait > deadline
