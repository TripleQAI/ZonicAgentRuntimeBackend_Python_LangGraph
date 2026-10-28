"""Error responses (design 14.2; cases A-09, A-11): one envelope ``{error: {code, message,
details, traceId}}`` with the catalog's status and user-safe message; ``traceId`` is the request ID.

* ``ZonicError``: its code (``details`` only for validation issues, never internal values);
* request validation: ``INPUT_INVALID`` with each problem's path (an unknown field included);
* an unknown route or method: ``NOT_FOUND``;
* anything else: ``INTERNAL``, with no detail.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from zonic_runtime.errors import ZonicError
from zonic_runtime.models.api.errors import ErrorBody, ErrorDetail, ErrorEnvelope
from zonic_runtime.models.resources.misc import ErrorCatalog

REQUEST_ID = "x-request-id"
UNKNOWN = "INTERNAL"
#: Request locations that are not part of the client's path: ``body.input.text`` -> ``input.text``.
_LOCATIONS = frozenset({"body", "query", "path", "header"})


def request_id(request: Request) -> str:
    found = getattr(request.state, "request_id", None)
    return found if isinstance(found, str) else str(uuid.uuid4())


def error_response(
    catalog: ErrorCatalog,
    code: str,
    trace_id: str | None,
    details: Sequence[ErrorDetail] = (),
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    entry = catalog.errors.get(code)
    if entry is None:
        code, entry = UNKNOWN, catalog.errors[UNKNOWN]
    body = ErrorEnvelope(
        error=ErrorBody(
            code=code,
            message=entry.message,
            details=list(details),
            trace_id=trace_id,
            retryable=entry.retryable,
        )
    )
    return JSONResponse(
        body.model_dump(mode="json", by_alias=True, exclude_none=True),
        status_code=entry.status,
        headers=dict(headers or {}),
    )


def _path(location: Sequence[Any]) -> str:
    parts = [str(p) for p in location]
    if parts and parts[0] in _LOCATIONS:
        parts = parts[1:]
    return ".".join(parts)


def _issues(error: ZonicError) -> list[ErrorDetail]:
    issues = error.details.get("issues")
    if not isinstance(issues, list):
        return []
    return [
        ErrorDetail(path=str(i.get("path", "")), message=str(i.get("message", "")))
        for i in issues
        if isinstance(i, Mapping)
    ]


def install(app: FastAPI, catalog: ErrorCatalog) -> None:
    async def zonic(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, ZonicError)
        headers = {}
        retry_after = exc.details.get("retry_after")
        if isinstance(retry_after, int):
            headers["Retry-After"] = str(retry_after)
        return error_response(catalog, exc.code, request_id(request), _issues(exc), headers)

    async def validation(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RequestValidationError)
        details = [
            ErrorDetail(
                path=_path(e.get("loc", ())), message=str(e.get("msg", "")), type=str(e.get("type", ""))
            )
            for e in exc.errors()
        ]
        return error_response(catalog, "INPUT_INVALID", request_id(request), details)

    async def http(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, HTTPException)
        code = (
            "NOT_FOUND"
            if exc.status_code in (404, 405)
            else "INPUT_INVALID"
            if exc.status_code < 500
            else UNKNOWN
        )
        return error_response(catalog, code, request_id(request))

    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        return error_response(catalog, UNKNOWN, request_id(request))

    app.add_exception_handler(ZonicError, zonic)
    app.add_exception_handler(RequestValidationError, validation)
    app.add_exception_handler(HTTPException, http)
    app.add_exception_handler(Exception, unexpected)


class RequestContext:
    """Pure ASGI middleware (it never buffers a streamed response): the request ID from
    ``X-Request-ID`` or a new one, echoed on the response; bodies above ``maxBodyBytes`` are
    ``INPUT_TOO_LARGE`` before the route reads them."""

    def __init__(self, app: ASGIApp, catalog: ErrorCatalog, max_body_bytes: int) -> None:
        self.app, self.catalog, self.max_body_bytes = app, catalog, max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        rid = headers.get(REQUEST_ID) or str(uuid.uuid4())
        scope.setdefault("state", {})["request_id"] = rid
        length = headers.get("content-length")
        if length is not None and length.isdigit() and int(length) > self.max_body_bytes:
            response = error_response(self.catalog, "INPUT_TOO_LARGE", rid, headers={REQUEST_ID: rid})
            await response(scope, receive, send)
            return

        async def tagged(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append((REQUEST_ID.encode(), rid.encode()))
            await send(message)

        await self.app(scope, receive, tagged)


ExceptionHandler = Callable[[Request, Exception], Awaitable[JSONResponse]]
#: Every ``/v1`` route documents the envelope as its error response.
ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    "default": {"description": "An error: a code of the error catalog", "model": ErrorEnvelope}
}
