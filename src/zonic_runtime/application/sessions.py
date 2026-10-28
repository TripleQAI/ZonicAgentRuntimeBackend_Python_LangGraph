"""Sessions and their messages (design 14.1; plan P2-03, cases A-03, A-04).

Every read carries the caller's owner filter, so another user's session is the same 404 as a
missing one (case A-03). Ported from the Node.js runtime's ``session-service.ts``.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from zonic_runtime.application.caller import Caller
from zonic_runtime.application.paging import (
    contains_pattern,
    decode_cursor,
    encode_cursor,
    invalid,
    page_size,
)
from zonic_runtime.application.runtime import WorkflowRuntime
from zonic_runtime.errors import ZonicError
from zonic_runtime.integrations.gateway.repository import GatewayRepository
from zonic_runtime.models.api.requests import (
    CreateSessionRequest,
    MessageVisibility,
    SessionListStatus,
    UpdateSessionRequest,
)
from zonic_runtime.models.api.responses import Message, MessageList, Session, SessionList
from zonic_runtime.models.gateway.runtime import (
    SESSION_ID_OUT,
    MessageRow,
    SessionCreateArgs,
    SessionSummaryRow,
    SessionUpdateArgs,
)
from zonic_runtime.models.gateway.wire import Filter, FilterOp, OrderBy
from zonic_runtime.models.resources.settings import RuntimeSettings

_ALL = "all"
_CHAT = "chat"


def to_session(row: SessionSummaryRow) -> Session:
    return Session(
        session_id=str(row.session_id),
        agentic_workflow_id=row.agentic_workflow_id,
        title=row.title,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
        last_message_at=row.last_message_at,
    )


def to_message(row: MessageRow) -> Message:
    return Message(
        message_id=str(row.message_id),
        session_id=str(row.session_id),
        run_id=str(row.run_id) if row.run_id else None,
        node_id=row.node_id,
        seq=row.seq,
        role=row.role,
        kind=row.kind,
        visibility=row.visibility,
        content_parts=row.content_parts,
        text_preview=row.text_preview,
        created_at=row.created_at,
        feedback_rating=row.feedback_rating,
    )


class SessionService:
    def __init__(
        self, repository: GatewayRepository, runtime: WorkflowRuntime, settings: RuntimeSettings
    ) -> None:
        self._repository = repository
        self._runtime = runtime
        self._settings = settings

    async def create(self, caller: Caller, request: CreateSessionRequest) -> Session:
        """Only for a workflow the caller may run."""
        await self._runtime.loader.load(request.agentic_workflow_id, caller.workflows())
        session = {
            "ownerSub": caller.subject,
            "ownerEmail": caller.email or "",
            "tenantId": caller.tenant or "",
            "agenticWorkflowId": request.agentic_workflow_id,
            "title": request.title or "",
        }
        out = await self._repository.call(SessionCreateArgs(session=session))
        return await self.get(caller, str(out[SESSION_ID_OUT]))

    async def row(self, caller: Caller, session_id: str) -> SessionSummaryRow:
        row = await self._repository.read_one(
            SessionSummaryRow, owner=caller.subject, filters=[Filter(column="session_id", value=session_id)]
        )
        if row is None:
            raise ZonicError("NOT_FOUND", session_id=session_id)
        return row

    async def get(self, caller: Caller, session_id: str) -> Session:
        return to_session(await self.row(caller, session_id))

    async def list(
        self,
        caller: Caller,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        q: str | None = None,
        status: SessionListStatus = "active",
    ) -> SessionList:
        """Newest first by creation time; keyset paging on ``(created_at, session_id)``. View
        filters are AND only, so a page starts at ``created_at <= cursor`` and drops the rows the
        previous page already returned (case A-04)."""
        size = page_size(limit, self._settings.api)
        filters: list[Filter] = []
        if status != _ALL:
            filters.append(Filter(column="status", value=status))
        if q:
            filters.append(Filter(column="title", op=FilterOp.ILIKE, value=contains_pattern(q)))
        key = decode_cursor(cursor, {"c": str, "i": str}) if cursor else None
        if key is not None:
            filters.append(Filter(column="created_at", op=FilterOp.LE, value=key["c"]))
        order = [OrderBy(column="created_at", descending=True), OrderBy(column="session_id", descending=True)]

        def seen(row: SessionSummaryRow) -> bool:
            return (
                key is not None and row.created_at.isoformat() == key["c"] and str(row.session_id) >= key["i"]
            )

        rows: list[SessionSummaryRow] = []
        offset = 0
        while len(rows) <= size:
            page = await self._repository.read(
                SessionSummaryRow,
                owner=caller.subject,
                filters=filters,
                order_by=order,
                limit=size + 1,
                offset=offset,
            )
            rows += [r for r in page if not seen(r)]
            if len(page) < size + 1:
                break
            offset += size + 1
        items = [to_session(r) for r in rows[:size]]
        next_cursor = None
        if len(rows) > size and items:
            last = rows[size - 1]
            next_cursor = encode_cursor({"c": last.created_at.isoformat(), "i": str(last.session_id)})
        return SessionList(items=items, next_cursor=next_cursor)

    async def update(self, caller: Caller, session_id: str, request: UpdateSessionRequest) -> Session:
        if request.title is None and request.status is None:
            raise invalid("", "title or status is required")
        patch: dict[str, Any] = {}
        if request.title is not None:
            patch["title"] = request.title
        if request.status is not None:
            patch["status"] = request.status
        await self._repository.call(
            SessionUpdateArgs(session_id=UUID(session_id), owner_sub=caller.subject, patch=patch)
        )
        return await self.get(caller, session_id)

    async def messages(
        self,
        caller: Caller,
        session_id: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        visibility: MessageVisibility = "chat",
    ) -> MessageList:
        """Newest first by ``seq``. ``visibility=internal`` adds agents' intermediate turns, for the
        groups in ``settings.auth.internalMessageRoles`` only."""
        await self.row(caller, session_id)
        internal = visibility != _CHAT
        if internal and not caller.has_any(self._settings.auth.internal_message_roles):
            raise ZonicError("FORBIDDEN", visibility=visibility)
        size = page_size(limit, self._settings.api)
        filters = [Filter(column="session_id", value=session_id)]
        if not internal:
            filters.append(Filter(column="visibility", value=_CHAT))
        if cursor:
            filters.append(Filter(column="seq", op=FilterOp.LT, value=decode_cursor(cursor, {"s": int})["s"]))
        rows = await self._repository.read(
            MessageRow,
            owner=caller.subject,
            filters=filters,
            order_by=[OrderBy(column="seq", descending=True)],
            limit=size + 1,
        )
        items = [to_message(r) for r in rows[:size]]
        next_cursor = encode_cursor({"s": items[-1].seq}) if len(rows) > size and items else None
        return MessageList(items=items, next_cursor=next_cursor)
