"""The cache service of ``ZONIC_CACHE_API_URL`` (plan P3-05), with the calls the Zonic MCP server
makes (``app/infra/cache.py``), after the Node.js runtime's ``HttpCacheStore``: ``GET keys/<key>``
answers ``{"data": {"value", "hit"}}``, ``PUT keys/<key>`` stores ``{"value", "ttl"}``. A key keeps
its ``:`` in the path, as the MCP server sends it. An unreachable service, or an answer other than
success or not found, is ``DATA_UNAVAILABLE``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx

from zonic_runtime.errors import ZonicError
from zonic_runtime.models.resources.settings import DatasetSettings

UNAVAILABLE = "DATA_UNAVAILABLE"


class HttpCacheStore:
    def __init__(self, base_url: str, settings: DatasetSettings, client: httpx.AsyncClient) -> None:
        self._base = base_url.rstrip("/")
        self._seconds = settings.cache_timeout_milliseconds / 1000
        self._client = client

    def _url(self, key: str) -> str:
        return f"{self._base}/keys/{quote(key, safe=':')}"

    async def _call(self, method: str, key: str, body: str | None = None) -> httpx.Response:
        try:
            return await self._client.request(
                method,
                self._url(key),
                content=body,
                headers={"content-type": "application/json"} if body is not None else None,
                timeout=self._seconds,
            )
        except httpx.HTTPError as exc:
            raise ZonicError(UNAVAILABLE, transport=True, reason="cache service unreachable") from exc

    async def get(self, key: str) -> Any:
        response = await self._call("GET", key)
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise ZonicError(UNAVAILABLE, status=response.status_code, reason="cache service error", key=key)
        try:
            body = response.json()
        except ValueError as exc:
            raise ZonicError(UNAVAILABLE, reason="cache service answered no JSON", key=key) from exc
        data = body.get("data") if isinstance(body, Mapping) else None
        if not isinstance(data, Mapping) or data.get("hit") is False:
            return None
        return data.get("value")

    async def put(self, key: str, value: Any, ttl_seconds: int) -> None:
        body = json.dumps({"value": value, "ttl": ttl_seconds}, ensure_ascii=False, default=str)
        response = await self._call("PUT", key, body)
        if response.status_code not in (200, 201):
            raise ZonicError(UNAVAILABLE, status=response.status_code, reason="cache service error", key=key)
