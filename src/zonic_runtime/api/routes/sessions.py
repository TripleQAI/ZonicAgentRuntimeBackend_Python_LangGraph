"""``/v1/sessions``: list, create, read, rename or archive, and a session's messages."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request

from zonic_runtime.api.deps import current_caller, deps_of
from zonic_runtime.api.errors import ERROR_RESPONSES
from zonic_runtime.application.caller import Caller
from zonic_runtime.models.api.requests import (
    CreateSessionRequest,
    MessageVisibility,
    SessionListStatus,
    UpdateSessionRequest,
)
from zonic_runtime.models.api.responses import MessageList, Session, SessionList

router = APIRouter(tags=["sessions"], responses=ERROR_RESPONSES)
CallerDep = Annotated[Caller, Depends(current_caller)]


@router.get(
    "/sessions",
    operation_id="listSessions",
    summary="The caller's sessions, newest first, with cursor paging and title search",
    response_model=SessionList,
    response_model_exclude_none=True,
)
async def list_sessions(
    request: Request,
    caller: CallerDep,
    cursor: Annotated[str | None, Query(max_length=500)] = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    status: SessionListStatus = "active",
) -> SessionList:
    return await deps_of(request).sessions.list(caller, cursor=cursor, limit=limit, q=q, status=status)


@router.post(
    "/sessions",
    operation_id="createSession",
    summary="A new session for a workflow",
    status_code=201,
    response_model=Session,
    response_model_exclude_none=True,
)
async def create_session(request: Request, caller: CallerDep, body: CreateSessionRequest) -> Session:
    return await deps_of(request).sessions.create(caller, body)


@router.get(
    "/sessions/{sessionId}",
    operation_id="getSession",
    summary="Session details",
    response_model=Session,
    response_model_exclude_none=True,
)
async def get_session(
    request: Request, caller: CallerDep, session_id: Annotated[UUID, Path(alias="sessionId")]
) -> Session:
    return await deps_of(request).sessions.get(caller, str(session_id))


@router.patch(
    "/sessions/{sessionId}",
    operation_id="updateSession",
    summary="Rename or archive a session",
    response_model=Session,
    response_model_exclude_none=True,
)
async def update_session(
    request: Request,
    caller: CallerDep,
    session_id: Annotated[UUID, Path(alias="sessionId")],
    body: UpdateSessionRequest,
) -> Session:
    return await deps_of(request).sessions.update(caller, str(session_id), body)


@router.get(
    "/sessions/{sessionId}/messages",
    operation_id="listMessages",
    summary="A session's messages, newest first, with content parts",
    response_model=MessageList,
    response_model_exclude_none=True,
)
async def list_messages(
    request: Request,
    caller: CallerDep,
    session_id: Annotated[UUID, Path(alias="sessionId")],
    cursor: Annotated[str | None, Query(max_length=500)] = None,
    limit: Annotated[int | None, Query(ge=1)] = None,
    visibility: MessageVisibility = "chat",
) -> MessageList:
    return await deps_of(request).sessions.messages(
        caller, str(session_id), cursor=cursor, limit=limit, visibility=visibility
    )
