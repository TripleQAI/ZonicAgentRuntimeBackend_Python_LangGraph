"""One generic repository driven by row and argument models (design Section 15.1, lever L9).

Reads name a view through ``@gateway_view``; writes name a procedure through
``@gateway_procedure``. Column names come from ``Column`` markers, positional
argument order is the argument model's field order, JSON values travel as JSON
text, and INOUT placeholders are appended as NULLs. Owner-scoped views always
get an owner filter: a read without an owner is a programming error.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, datetime
from enum import Enum
from typing import Any, TypeVar
from uuid import UUID

from pydantic import BaseModel, ValidationError

from zonic_runtime.errors import ZonicError
from zonic_runtime.models.gateway.wire import BatchItem, Filter, FilterOp, OrderBy, QueryResult
from zonic_runtime.models.gateway_meta import procedure_meta, view_meta
from zonic_runtime.models.markers import column_map
from zonic_runtime.ports.gateway import GatewayPort

R = TypeVar("R", bound=BaseModel)

#: Logical schema key -> logical datasource key. Builder views live in the runtime's database.
_DATASOURCE_FOR_SCHEMA = {"runtime": "runtime", "builder": "runtime", "observability": "observability"}


VIEW_OP = "view"  # principle: allow rule-1-node-type gateway-operation-name


def datasource_for(schema_key: str) -> str:
    try:
        return _DATASOURCE_FOR_SCHEMA[schema_key]
    except KeyError as exc:
        raise ValueError(f"unknown gateway schema key {schema_key!r}") from exc


def encode_argument(value: Any) -> Any:
    """Scalars pass through; JSON values are sent as JSON text (the gateway's jsonb convention)."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (UUID, Enum)):
        return str(value.value if isinstance(value, Enum) else value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return json.dumps(value.model_dump(mode="json", by_alias=True, exclude_none=True))
    return json.dumps(value, default=str)


def procedure_arguments(args: BaseModel) -> list[Any]:
    meta = procedure_meta(type(args))
    values = [encode_argument(getattr(args, name)) for name in type(args).model_fields]
    return [*values, *([None] * meta.placeholders)]


class GatewayRepository:
    def __init__(self, gateway: GatewayPort) -> None:
        self._gateway = gateway

    # -- reads ---------------------------------------------------------------

    def _view_request(
        self, row_model: type[BaseModel], owner: str | None, filters: Sequence[Filter] | None
    ) -> tuple[str, str, str, list[str], list[Filter], dict[str, str]]:
        meta = view_meta(row_model)
        columns = column_map(row_model)
        all_filters = list(filters or [])
        if meta.owner_column is not None:
            if owner is None:
                raise ValueError(f"{row_model.__name__} is owner-scoped; pass owner=")
            all_filters.append(Filter(column=meta.owner_column, op=FilterOp.EQ, value=owner))
        return (
            datasource_for(meta.schema_key),
            meta.schema_key,
            meta.name,
            list(columns.values()),
            all_filters,
            columns,
        )

    @staticmethod
    def parse_rows(row_model: type[R], rows: list[dict[str, Any]]) -> list[R]:
        columns = column_map(row_model)
        parsed: list[R] = []
        for row in rows:
            data = {field: row[column] for field, column in columns.items() if column in row}
            try:
                parsed.append(row_model.model_validate(data))
            except ValidationError as exc:
                raise ZonicError(
                    "DATA_REQUEST_INVALID", model=row_model.__name__, problems=exc.errors(include_url=False)
                ) from exc
        return parsed

    async def read(
        self,
        row_model: type[R],
        *,
        owner: str | None = None,
        filters: Sequence[Filter] | None = None,
        order_by: Sequence[OrderBy] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[R]:
        datasource, schema, name, columns, all_filters, _ = self._view_request(row_model, owner, filters)
        result = await self._gateway.view(
            datasource,
            schema,
            name,
            columns=columns,
            filters=all_filters,
            order_by=list(order_by or []),
            limit=limit,
            offset=offset,
        )
        return self.parse_rows(row_model, result.rows)

    async def read_one(
        self, row_model: type[R], *, owner: str | None = None, filters: Sequence[Filter]
    ) -> R | None:
        rows = await self.read(row_model, owner=owner, filters=filters, limit=2)
        if len(rows) > 1:
            raise ZonicError("DATA_REQUEST_INVALID", model=row_model.__name__, reason="more than one row")
        return rows[0] if rows else None

    def view_item(
        self,
        item_id: str,
        row_model: type[BaseModel],
        *,
        owner: str | None = None,
        filters: Sequence[Filter] | None = None,
        order_by: Sequence[OrderBy] | None = None,
        limit: int | None = None,
    ) -> BatchItem:
        datasource, schema, name, columns, all_filters, _ = self._view_request(row_model, owner, filters)
        payload: dict[str, Any] = {
            "name": name,
            "schema": schema,
            "columns": columns,
            "filters": [f.model_dump(mode="json") for f in all_filters],
            "order_by": [o.model_dump(mode="json") for o in order_by or []],
        }
        if limit is not None:
            payload["limit"] = limit
        return BatchItem(id=item_id, datasource=datasource, op=VIEW_OP, payload=payload)

    # -- writes --------------------------------------------------------------

    async def call(self, args: BaseModel) -> dict[str, Any]:
        """Call the argument model's procedure; returns its INOUT outputs (the first row)."""
        meta = procedure_meta(type(args))
        result: QueryResult = await self._gateway.procedure(
            datasource_for(meta.schema_key),
            meta.schema_key,
            meta.name,
            procedure_arguments(args),
            retry=meta.idempotent,
        )
        return result.first() or {}

    def procedure_item(self, item_id: str, args: BaseModel) -> BatchItem:
        meta = procedure_meta(type(args))
        return BatchItem(
            id=item_id,
            datasource=datasource_for(meta.schema_key),
            op="procedure",
            payload={"name": meta.name, "schema": meta.schema_key, "args": procedure_arguments(args)},
        )
