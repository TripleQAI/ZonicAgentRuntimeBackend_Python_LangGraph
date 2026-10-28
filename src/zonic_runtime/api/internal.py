"""The internal app on ``api.internalPort`` (design 14.1; plan P2-05): ``/metrics`` for the
autoscaler, and ``/internal/runs/{runId}/events``: the events of a run this instance drives, which
other instances proxy for their subscribers (P4-05). No token: the port is reachable only inside the
service's network, never through the load balancer.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, Response

from zonic_runtime.api.deps import AppDeps
from zonic_runtime.api.sse import sse_response
from zonic_runtime.runtime.metrics import CONTENT_TYPE

_LAST_EVENT_ID = "Last-Event-ID"


def create_internal_app(deps: AppDeps) -> FastAPI:
    app = FastAPI(title="Zonic Agent Runtime internal", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(deps.metrics.render(), media_type=CONTENT_TYPE)

    @app.get("/internal/runs/{run_id}/events", include_in_schema=False)
    async def run_events(run_id: str, request: Request) -> Response:
        """The events of a run this instance drives, for another instance's subscriber (Q-14)."""
        last = request.headers.get(_LAST_EVENT_ID, "")
        events = deps.runs.local_events(run_id, int(last) if last.isdigit() else 0)
        if events is None:
            return Response(status_code=404)
        settings = deps.resources.settings
        return sse_response(
            events,
            run_id,
            settings.sse.heartbeat_seconds,
            deps.clock,
            queue_events=settings.stream.subscriber_queue_events,
        )

    return app
