"""The SSE writer (design 10, 14.2; cases S-01, S-06, S-07, S-08).

A ``StreamingResponse`` of ``text/event-stream`` with no caching and no proxy buffering. Each
event is ``id: <seq>``, ``event: <type>``, ``data: <envelope>``, so a reconnecting client sends
``Last-Event-ID``. While no event arrives for ``sse.heartbeatSeconds`` a ``heartbeat`` event is
sent (it carries no ``id``, so it never moves the client's position). A disconnect stops only
this response: the run goes on and is saved (``runs.cancelOnDisconnect`` is false).

Between the run's stream and the response is a queue of ``stream.subscriberQueueEvents``: a slow
client holds back only its own reader, which falls behind the replay buffer and gets
``stream.gap``; the run never waits for it (case Q-05).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi.responses import StreamingResponse

from zonic_runtime.runtime.clock import Clock

HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}
MEDIA_TYPE = "text/event-stream"
HEARTBEAT = "heartbeat"
_DONE = object()


def frame(event: dict[str, Any], with_id: bool = True) -> bytes:
    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str)
    lines = [f"id: {event['seq']}"] if with_id and "seq" in event else []
    lines += [f"event: {event['type']}", f"data: {data}", "", ""]
    return "\n".join(lines).encode("utf-8")


async def _pump(events: AsyncIterator[dict[str, Any]], queue: asyncio.Queue[Any]) -> None:
    try:
        async for event in events:
            await queue.put(event)
    finally:
        task = asyncio.current_task()
        if task is None or not task.cancelling():  # a cancelled pump has no reader left
            await queue.put(_DONE)


async def sse_frames(
    events: AsyncIterator[dict[str, Any]],
    run_id: str,
    heartbeat_seconds: float,
    clock: Clock,
    queue_events: int = 1000,
) -> AsyncIterator[bytes]:
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=queue_events)
    pump = asyncio.create_task(_pump(events, queue))
    last_seq = 0
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=heartbeat_seconds)
            except TimeoutError:
                beat = {
                    "runId": run_id,
                    "seq": max(last_seq, 1),
                    "type": HEARTBEAT,
                    "ts": clock.now().isoformat(),
                    "data": {},
                }
                yield frame(beat, with_id=False)
                continue
            if item is _DONE:
                return
            last_seq = int(item.get("seq", last_seq))
            yield frame(item)
    finally:
        pump.cancel()


def sse_response(
    events: AsyncIterator[dict[str, Any]],
    run_id: str,
    heartbeat_seconds: float,
    clock: Clock,
    headers: dict[str, str] | None = None,
    queue_events: int = 1000,
) -> StreamingResponse:
    return StreamingResponse(
        sse_frames(events, run_id, heartbeat_seconds, clock, queue_events),
        media_type=MEDIA_TYPE,
        headers={**HEADERS, **(headers or {})},
    )
