"""HTTP adapter for the pgfoundation gateway; the only module that speaks its URLs and statuses.

Retry policy: reads retry on 503 and transport failures with exponential backoff;
procedures never retry here unless the caller marks the call idempotent (design
Section 15.4). The gateway already retries serialization failures inside a
transaction, so a second layer for writes would re-run committed work.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.errors import ZonicError
from zonic_runtime.models.gateway.wire import (
    BatchItem,
    BatchMode,
    BatchRequest,
    BatchResult,
    Filter,
    OrderBy,
    ProcedureIn,
    QueryResult,
    ViewIn,
)
from zonic_runtime.models.resources.settings import GatewaySettings

_RETRYABLE_STATUS = frozenset({503})

#: Reason tokens the procedures raise as the whole message (fn_raise, db/runtime/005 header)
#: -> catalog code. The gateway passes the database message through, DETAIL on later lines.
_CONFLICT_REASONS = {
    "owner_mismatch": "NOT_FOUND",
    "not_found": "NOT_FOUND",
    "session_not_found": "NOT_FOUND",
    "run_not_found": "NOT_FOUND",
    "interrupt_not_found": "NOT_FOUND",
    "run_in_progress": "RUN_IN_PROGRESS",
    "run_limit_reached": "RATE_LIMITED",
    "stale_attempt": "RUN_LEASE_LOST",
    "invalid_transition": "DATA_CONFLICT",
    "already_finished": "DATA_CONFLICT",
    "version_conflict": "DATA_CONFLICT",
    "already_answered": "INTERRUPT_ALREADY_ANSWERED",
    "interrupt_expired": "INTERRUPT_EXPIRED",
    "interrupt_closed": "INTERRUPT_EXPIRED",
}
_REASON_RE = re.compile(r"\s*([a-z_]+)\b")


def translate_status(status: int, body: Any) -> ZonicError:
    """Gateway status -> catalog code. Gateway error vocabulary never leaves this module."""
    message = body.get("error", "") if isinstance(body, dict) else str(body)
    kind = body.get("type", "") if isinstance(body, dict) else ""
    if status == 409:
        found = _REASON_RE.match(message)
        code = _CONFLICT_REASONS.get(found.group(1)) if found else None
        return ZonicError(code) if code else ZonicError("DATA_CONFLICT", gateway_type=kind)
    if status == 503:
        return ZonicError("DATA_UNAVAILABLE", gateway_status=status)
    return ZonicError("DATA_REQUEST_INVALID", gateway_status=status, gateway_type=kind)


class HttpGateway:
    """Implements ``GatewayPort`` over the gateway's governed v1 surface."""

    def __init__(
        self, settings: GatewaySettings, env: EnvironmentModel, client: httpx.AsyncClient | None = None
    ) -> None:
        self._settings = settings
        self._owns_client = client is None
        headers = {"content-type": "application/json"}
        if env.gateway_api_key is not None:
            headers["authorization"] = f"Bearer {env.gateway_api_key.get_secret_value()}"
        self._client = client or httpx.AsyncClient(
            base_url=env.gateway_base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(
                settings.timeout_milliseconds / 1000, connect=settings.connect_timeout_milliseconds / 1000
            ),
            limits=httpx.Limits(
                max_connections=settings.max_in_flight, max_keepalive_connections=settings.max_in_flight
            ),
        )
        self._in_flight = asyncio.Semaphore(settings.max_in_flight)

    # -- names ---------------------------------------------------------------

    def datasource(self, key: str) -> str:
        return str(getattr(self._settings.datasources, key))

    def schema(self, key: str) -> str:
        return str(getattr(self._settings.schemas, key))

    # -- transport -----------------------------------------------------------

    async def _post(self, path: str, payload: dict[str, Any], *, retry: bool, request_id: str | None) -> Any:
        attempts = self._settings.read_retries + 1 if retry else 1
        headers = {"x-request-id": request_id} if request_id else None
        last: ZonicError | None = None
        for attempt in range(attempts):
            try:
                async with self._in_flight:
                    response = await self._client.post(path, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                last = ZonicError("DATA_UNAVAILABLE", transport=type(exc).__name__)
            else:
                if response.status_code < 400:
                    return response.json()
                body: Any
                try:
                    body = response.json()
                except ValueError:
                    body = response.text
                last = translate_status(response.status_code, body)
                if response.status_code not in _RETRYABLE_STATUS:
                    raise last
            if attempt + 1 < attempts:
                await asyncio.sleep(self._settings.retry_backoff_milliseconds / 1000 * (2**attempt))
        assert last is not None
        raise last

    # -- operations ----------------------------------------------------------

    async def view(
        self,
        datasource: str,
        schema: str,
        name: str,
        *,
        columns: list[str] | None = None,
        filters: list[Filter] | None = None,
        order_by: list[OrderBy] | None = None,
        limit: int | None = None,
        offset: int | None = None,
        request_id: str | None = None,
    ) -> QueryResult:
        capped = min(limit, self._settings.max_view_rows) if limit else self._settings.max_view_rows
        payload = ViewIn(
            name=name,
            db_schema=self.schema(schema),
            columns=columns,
            filters=filters or [],
            order_by=order_by or [],
            limit=capped,
            offset=offset,
        ).model_dump(mode="json", by_alias=True, exclude_none=True)
        data = await self._post(
            f"/v1/datasources/{self.datasource(datasource)}/view", payload, retry=True, request_id=request_id
        )
        return QueryResult.model_validate(data)

    async def procedure(
        self,
        datasource: str,
        schema: str,
        name: str,
        args: list[Any],
        *,
        retry: bool = False,
        request_id: str | None = None,
    ) -> QueryResult:
        payload = ProcedureIn(name=name, db_schema=self.schema(schema), args=args).model_dump(
            mode="json", by_alias=True, exclude_none=False
        )
        data = await self._post(
            f"/v1/datasources/{self.datasource(datasource)}/procedure",
            payload,
            retry=retry,
            request_id=request_id,
        )
        return QueryResult.model_validate(data)

    async def batch(
        self, items: list[BatchItem], *, mode: BatchMode, retry: bool = False, request_id: str | None = None
    ) -> BatchResult:
        wire_items = []
        for item in items:
            payload = dict(item.payload)
            if "schema" in payload:
                payload["schema"] = self.schema(payload["schema"])
            wire_items.append(
                item.model_copy(update={"datasource": self.datasource(item.datasource), "payload": payload})
            )
        body = BatchRequest(mode=mode, items=wire_items).model_dump(mode="json")
        data = await self._post("/v1/batch", body, retry=retry, request_id=request_id)
        return BatchResult.model_validate(data)

    async def ready(self) -> bool:
        try:
            response = await self._client.get("/v1/ready")
        except httpx.HTTPError:
            return False
        return response.status_code < 400

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
