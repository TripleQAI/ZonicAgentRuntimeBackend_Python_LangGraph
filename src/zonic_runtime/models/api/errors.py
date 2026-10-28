"""The envelope of every error response (design 14.2): ``{error: {code, message, details, traceId}}``."""

from __future__ import annotations

from pydantic import Field

from zonic_runtime.models.base import ZonicModel


class ErrorDetail(ZonicModel):
    """One validation problem: the request path and a short message; never internal details."""

    path: str
    message: str
    type: str | None = None


class ErrorBody(ZonicModel):
    #: A code from ``resources/error-catalog.json``.
    code: str
    #: The user-safe message of the code.
    message: str
    details: list[ErrorDetail] = Field(default_factory=list)
    #: The request's correlation ID (the trace ID of run requests).
    trace_id: str | None = None
    #: Whether repeating the request may succeed.
    retryable: bool | None = None


class ErrorEnvelope(ZonicModel):
    error: ErrorBody
