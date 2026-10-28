"""``DatasetService``: ``GET /v1/datasets/{datasetId}`` (design 12.2; plan P3-04, P3-05), after the
Node.js runtime's ``dataset-service.ts``.

A page of a dataset the caller's runs produced. The metadata row is read with the owner filter, so
another user's dataset is the same 404 as a missing one (case X-01); the data comes from the cache
service or from the ``database`` store's chunks. ``filter`` is a JSON object of field to text (a row
matches when every field contains its text, case-insensitive); ``sort`` is ``field`` or ``field:desc``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from zonic_runtime.application.caller import Caller
from zonic_runtime.errors import ZonicError
from zonic_runtime.integrations.gateway.repository import GatewayRepository
from zonic_runtime.io.rows import columns_of
from zonic_runtime.models.api.responses import DatasetPage
from zonic_runtime.models.gateway.runtime import DatasetRow
from zonic_runtime.models.gateway.wire import Filter, FilterOp, OrderBy
from zonic_runtime.models.resources.settings import RuntimeSettings
from zonic_runtime.ports.datasets import CacheStorePort

NOT_FOUND, UNAVAILABLE, INVALID = "DATASET_NOT_FOUND", "DATA_UNAVAILABLE", "INPUT_INVALID"
_HTTP = "http_response"
_NUMBER = re.compile(r"(\d+)")

Row = dict[str, Any]


def rows_of(kind: str, value: Any) -> list[Row]:
    """A dataset's value as rows: rows as they are, an HTTP body that is a JSON list (or holds
    one), else one row."""

    def rows(items: Sequence[Any]) -> list[Row]:
        return [dict(i) if isinstance(i, Mapping) else {"value": i} for i in items]

    if isinstance(value, list):
        return rows(value)
    if kind == _HTTP and isinstance(value, Mapping) and isinstance(value.get("body"), str):
        try:
            parsed = json.loads(value["body"])
        except ValueError:
            return [{"body": value["body"]}]
        if isinstance(parsed, list):
            return rows(parsed)
        if isinstance(parsed, Mapping):
            found = next((v for v in parsed.values() if isinstance(v, list)), None)
            return rows(found) if found is not None else [dict(parsed)]
        return [{"body": value["body"]}]
    if value is None:
        return []
    return [dict(value) if isinstance(value, Mapping) else {"value": value}]


def joined(kind: str, chunks: Sequence[DatasetRow]) -> Any:
    """The ``database`` store's chunks joined back (``dataset_store.chunks_of``)."""
    bodies = [c.body for c in chunks]
    if kind == _HTTP:
        parts = [b for b in bodies if isinstance(b, Mapping)]
        if not parts:
            return None
        return {**parts[0], "body": "".join(str(p.get("body") or "") for p in parts)}
    found: list[Any] = []
    for body in bodies:
        found.extend(body if isinstance(body, list) else [body])
    return found


def _natural(value: Any) -> tuple[Any, ...]:
    """Numbers by value; text by its digit runs as numbers, the rest case-folded (``localeCompare``
    with ``numeric: true``)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        text = "" if value is None else str(value)
        return (1, *((0, int(p)) if p.isdigit() else (1, p.casefold()) for p in _NUMBER.split(text) if p))
    return (0, value)


class DatasetService:
    def __init__(
        self, repository: GatewayRepository, settings: RuntimeSettings, cache: CacheStorePort | None = None
    ) -> None:
        self._repository, self._settings, self._cache = repository, settings, cache

    async def page(
        self,
        caller: Caller,
        dataset_id: str,
        *,
        page: int | None = None,
        page_size: int | None = None,
        sort: str | None = None,
        filter: str | None = None,
    ) -> DatasetPage:
        try:
            UUID(dataset_id)
        except ValueError as exc:
            raise ZonicError(NOT_FOUND, dataset_id=dataset_id) from exc
        by_id = Filter(column="dataset_id", value=dataset_id)
        meta = await self._repository.read_one(
            DatasetRow, owner=caller.subject, filters=[by_id, Filter(column="chunk_no", value=0)]
        )
        if meta is None:
            raise ZonicError(NOT_FOUND, dataset_id=dataset_id)
        if meta.store == "cache":
            if self._cache is None:
                raise ZonicError(UNAVAILABLE, reason="no cache service is configured")
            value = await self._cache.get(meta.cache_key or "")
            if value is None:
                raise ZonicError(NOT_FOUND, dataset_id=dataset_id, reason="expired")
        else:
            chunks = await self._repository.read(
                DatasetRow,
                owner=caller.subject,
                filters=[by_id, Filter(column="chunk_no", op=FilterOp.GE, value=1)],
                order_by=[OrderBy(column="chunk_no")],
                limit=self._settings.gateway.max_view_rows,
            )
            value = joined(meta.kind, chunks)
        rows = self._sorted(self._filtered(rows_of(meta.kind, value), filter), sort)
        api = self._settings.api
        number = max(1, page or 1)
        size = min(api.max_page_size, max(1, page_size or api.default_page_size))
        columns = (
            [str(c) for c in meta.columns]
            if meta.columns
            else columns_of(rows, self._settings.io.column_sample_rows)
        )
        return DatasetPage(
            dataset_id=dataset_id,
            kind=meta.kind,
            rows=rows[(number - 1) * size : number * size],
            total=len(rows),
            page=number,
            page_size=size,
            columns=columns,
        )

    @staticmethod
    def _filtered(rows: list[Row], text: str | None) -> list[Row]:
        if not text:
            return rows
        try:
            wanted = json.loads(text)
        except ValueError as exc:
            raise ZonicError(INVALID, field="filter", reason="must be a JSON object") from exc
        if not isinstance(wanted, Mapping):
            raise ZonicError(INVALID, field="filter", reason="must be a JSON object")
        terms = [(str(k), str(v).lower()) for k, v in wanted.items()]
        return [
            r
            for r in rows
            if all(t in str(r.get(f) if r.get(f) is not None else "").lower() for f, t in terms)
        ]

    @staticmethod
    def _sorted(rows: list[Row], sort: str | None) -> list[Row]:
        if not sort:
            return rows
        field, _, direction = sort.partition(":")
        return sorted(rows, key=lambda r: _natural(r.get(field)), reverse=direction == "desc")
