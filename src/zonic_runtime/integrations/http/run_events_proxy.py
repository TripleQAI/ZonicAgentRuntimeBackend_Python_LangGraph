"""``RunEventsProxy``: a run's events from the instance that drives it (design 9.6; plan P4-05;
case Q-14), after the Node.js runtime's ``run-events-proxy.ts``.

A subscriber whose request reached another instance reads the run's events through the driving
instance's internal route, ``/internal/runs/{runId}/events`` (its lease names the address), after
its ``Last-Event-ID``. The events come back as they are, so seq, replay and ``stream.gap`` behave
as on the driving instance and a resumed subscriber gets no duplicates.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

INTERNAL_EVENTS = "/internal/runs/{run_id}/events"
_DATA = "data: "
_LAST_EVENT_ID = "Last-Event-ID"
_HEARTBEAT = "heartbeat"


class RunEventsProxy:
    def __init__(self, client: Callable[[], httpx.AsyncClient]) -> None:
        self._client = client

    async def events(self, address: str, run_id: str, after: int) -> AsyncIterator[dict[str, Any]]:
        url = address.rstrip("/") + INTERNAL_EVENTS.format(run_id=run_id)
        headers = {_LAST_EVENT_ID: str(after)} if after else {}
        async with self._client().stream("GET", url, headers=headers) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith(_DATA):
                    continue
                event = json.loads(line[len(_DATA) :])
                if isinstance(event, dict) and event.get("type") != _HEARTBEAT:
                    yield event
