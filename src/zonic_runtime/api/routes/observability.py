"""Traces, usage and feedback (P3-11): ``/v1/runs/{runId}/trace``, ``/trace-link``,
``/v1/observability/usage`` (trace roles only) and ``/v1/messages/{messageId}/feedback``."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Request, Response

from zonic_runtime.api.deps import current_caller, deps_of
from zonic_runtime.api.errors import ERROR_RESPONSES
from zonic_runtime.application.caller import Caller
from zonic_runtime.models.api.observability import Trace, TraceLink, UsageList
from zonic_runtime.models.api.requests import FeedbackRequest

router = APIRouter(tags=["observability"], responses=ERROR_RESPONSES)
CallerDep = Annotated[Caller, Depends(current_caller)]
RunId = Annotated[str, Path(alias="runId", max_length=100)]


@router.get(
    "/runs/{runId}/trace",
    operation_id="getRunTrace",
    summary="The run's trace: its observation tree and scores (trace roles only)",
    response_model=Trace,
    response_model_exclude_none=True,
)
async def get_trace(request: Request, caller: CallerDep, run_id: RunId) -> Trace:
    return await deps_of(request).traces.trace(caller, run_id)


@router.get(
    "/runs/{runId}/trace-link",
    operation_id="getRunTraceLink",
    summary="The run's trace on the Langfuse server, when one is configured (trace roles only)",
    response_model=TraceLink,
)
async def get_trace_link(request: Request, caller: CallerDep, run_id: RunId) -> TraceLink:
    return await deps_of(request).traces.link(caller, run_id)


@router.get(
    "/observability/usage",
    operation_id="getUsage",
    summary="Daily usage by node, or by model for the caller's tenant (trace roles only)",
    response_model=UsageList,
    response_model_exclude_none=True,
)
async def get_usage(
    request: Request,
    caller: CallerDep,
    group_by: Annotated[Literal["node", "model"], Query(alias="groupBy")] = "node",
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
) -> UsageList:
    return await deps_of(request).traces.usage(caller, group_by=group_by, start=start, end=end)


@router.post(
    "/messages/{messageId}/feedback",
    operation_id="saveFeedback",
    summary="A rating and comment on one of the caller's messages",
    status_code=204,
    response_class=Response,
)
async def save_feedback(
    request: Request,
    caller: CallerDep,
    message_id: Annotated[str, Path(alias="messageId", max_length=100)],
    body: FeedbackRequest,
) -> Response:
    await deps_of(request).feedback.save(caller, message_id, body)
    return Response(status_code=204)
