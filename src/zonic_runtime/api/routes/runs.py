"""``/v1/runs``: start a run (stream, sync or async), its status, its node timeline, its live
events, its cancellation (P4-04), and its interrupts: list them and answer one, which resumes the run
(P4-02)."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Path, Request, Response
from fastapi.responses import JSONResponse

from zonic_runtime.api.deps import current_caller, deps_of
from zonic_runtime.api.errors import ERROR_RESPONSES
from zonic_runtime.api.sse import MEDIA_TYPE, sse_response
from zonic_runtime.application.caller import Caller
from zonic_runtime.application.paging import invalid
from zonic_runtime.application.runs import StartedStream
from zonic_runtime.models.api.requests import AnswerInterruptRequest, StartRunRequest
from zonic_runtime.models.api.responses import NodeVisitList, Run, RunAccepted, RunInterruptList, RunResult

router = APIRouter(tags=["runs"], responses=ERROR_RESPONSES)
CallerDep = Annotated[Caller, Depends(current_caller)]
_STREAM_DOC: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "`stream`: the run events (server-sent `RunEvent`s); `sync`: the finished turn",
        "content": {MEDIA_TYPE: {"schema": {"$ref": "#/components/schemas/RunEvent"}}},
    },
    202: {"description": "`sync`: the run goes on after `runs.syncWaitSeconds`", "model": RunAccepted},
}


def _json(model: Any, status: int = 200, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        model.model_dump(mode="json", by_alias=True, exclude_none=True), status_code=status, headers=headers
    )


@router.post(
    "/runs",
    operation_id="startRun",
    summary="Start a run in stream or sync mode",
    response_model=RunResult,
    response_model_exclude_none=True,
    responses=_STREAM_DOC,
)
async def start_run(
    request: Request,
    caller: CallerDep,
    body: StartRunRequest,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Response:
    deps = deps_of(request)
    return _started(deps, await deps.runs.start(caller, body, idempotency_key))


def _started(deps: Any, started: Any) -> Response:
    """A started or resumed turn: its events (``stream``), its result (``sync``), or ``202``."""
    if isinstance(started, StartedStream):
        headers = {"X-Run-Id": started.run_id, "X-Session-Id": started.session_id}
        settings = deps.resources.settings
        return sse_response(
            started.events,
            started.run_id,
            settings.sse.heartbeat_seconds,
            deps.clock,
            headers,
            settings.stream.subscriber_queue_events,
        )
    if isinstance(started, RunAccepted):
        return _json(started, 202)
    return _json(started)


@router.get(
    "/runs/{runId}",
    operation_id="getRun",
    summary="Run status and result",
    response_model=Run,
    response_model_exclude_none=True,
    responses={304: {"description": "Unchanged since the given ETag"}},
)
async def get_run(
    request: Request,
    caller: CallerDep,
    run_id: Annotated[UUID, Path(alias="runId")],
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    run, etag = await deps_of(request).runs.get(caller, str(run_id))
    if if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag})
    headers = {"ETag": etag}
    if run.status in ("queued", "running"):
        headers["Retry-After"] = "1"
    return _json(run, headers=headers)


@router.get(
    "/runs/{runId}/events",
    operation_id="getRunEvents",
    summary="The run's live events; resumes after Last-Event-ID",
    responses={200: {"description": "Server-sent run events", "content": {MEDIA_TYPE: {}}}},
)
async def run_events(
    request: Request,
    caller: CallerDep,
    run_id: Annotated[UUID, Path(alias="runId")],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> Response:
    if last_event_id is not None and not last_event_id.strip().isdigit():
        raise invalid("Last-Event-ID", "must be an event seq")
    deps = deps_of(request)
    events = await deps.runs.events(caller, str(run_id), int(last_event_id) if last_event_id else None)
    settings = deps.resources.settings
    return sse_response(
        events,
        str(run_id),
        settings.sse.heartbeat_seconds,
        deps.clock,
        queue_events=settings.stream.subscriber_queue_events,
    )


@router.get(
    "/runs/{runId}/nodes",
    operation_id="listRunNodes",
    summary="The run's node timeline: status, duration, handles and error code per visit",
    response_model=NodeVisitList,
    response_model_exclude_none=True,
)
async def run_nodes(
    request: Request, caller: CallerDep, run_id: Annotated[UUID, Path(alias="runId")]
) -> NodeVisitList:
    return await deps_of(request).runs.nodes(caller, str(run_id))


@router.post(
    "/runs/{runId}/cancel",
    operation_id="cancelRun",
    summary="Cancel a run that is queued, running or waiting; a finished run is returned as it is",
    status_code=202,
    response_model=Run,
    response_model_exclude_none=True,
)
async def cancel_run(
    request: Request, caller: CallerDep, run_id: Annotated[UUID, Path(alias="runId")]
) -> Response:
    return _json(await deps_of(request).runs.cancel(caller, str(run_id)), 202)


@router.get(
    "/runs/{runId}/interrupts",
    operation_id="listRunInterrupts",
    summary="The run's pending interrupts, oldest first",
    response_model=RunInterruptList,
    response_model_exclude_none=True,
)
async def list_interrupts(
    request: Request, caller: CallerDep, run_id: Annotated[UUID, Path(alias="runId")]
) -> RunInterruptList:
    return await deps_of(request).interrupts.list(caller, str(run_id))


@router.post(
    "/runs/{runId}/interrupts/{interruptId}",
    operation_id="answerRunInterrupt",
    summary="Answer an interrupt; the run resumes with the answer in the requested mode",
    response_model=RunResult,
    response_model_exclude_none=True,
    responses=_STREAM_DOC,
)
async def answer_interrupt(
    request: Request,
    caller: CallerDep,
    run_id: Annotated[UUID, Path(alias="runId")],
    interrupt_id: Annotated[str, Path(alias="interruptId", max_length=100)],
    body: AnswerInterruptRequest,
) -> Response:
    deps = deps_of(request)
    return _started(deps, await deps.interrupts.answer(caller, str(run_id), interrupt_id, body))
