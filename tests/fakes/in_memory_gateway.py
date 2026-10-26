"""``InMemoryGateway``: the ``GatewayPort`` contract without a network or a database.

Views are lists of rows keyed by (schema key, view name); procedures are async
handlers keyed the same way. Filter semantics, ordering, limits and the three
batch modes follow pgfoundation; a transactional batch rolls back every table on
an error. Every call is recorded so tests can count gateway round trips (P-05).
"""

from __future__ import annotations

import asyncio
import copy
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from zonic_runtime.errors import ZonicError
from zonic_runtime.models.gateway.wire import (
    BatchItem,
    BatchItemError,
    BatchItemResult,
    BatchMode,
    BatchResult,
    Filter,
    FilterOp,
    OrderBy,
    QueryResult,
)

Handler = Callable[["InMemoryGateway", list[Any]], Awaitable[list[dict[str, Any]]]]


@dataclass
class Call:
    kind: str  # "view" | "procedure" | "batch:<mode>"
    name: str
    payload: Any = None


@dataclass
class InMemoryGateway:
    tables: dict[tuple[str, str], list[dict[str, Any]]] = field(default_factory=dict)
    procedures: dict[tuple[str, str], Handler] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)
    _faults: list[tuple[str, ZonicError]] = field(default_factory=list)

    # -- set-up --------------------------------------------------------------

    def put_rows(self, schema: str, view: str, rows: list[dict[str, Any]]) -> None:
        self.tables.setdefault((schema, view), []).extend(copy.deepcopy(rows))

    def on_procedure(self, schema: str, name: str, handler: Handler) -> None:
        self.procedures[(schema, name)] = handler

    def fail_next(self, name: str, error: ZonicError, times: int = 1) -> None:
        """The next ``times`` calls of view or procedure ``name`` raise ``error``."""
        self._faults.extend([(name, error)] * times)

    def count(self, kind_prefix: str) -> int:
        return sum(1 for c in self.calls if c.kind.startswith(kind_prefix))

    def _maybe_fail(self, name: str) -> None:
        for i, (fault_name, error) in enumerate(self._faults):
            if fault_name == name:
                del self._faults[i]
                raise error

    # -- reads ---------------------------------------------------------------

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
    ) -> QueryResult:
        self.calls.append(Call("view", name, {"filters": filters, "order_by": order_by, "limit": limit}))
        return self._view(schema, name, columns, filters, order_by, limit, offset)

    def _view(
        self,
        schema: str,
        name: str,
        columns: list[str] | None,
        filters: list[Filter] | None,
        order_by: list[OrderBy] | None,
        limit: int | None,
        offset: int | None,
    ) -> QueryResult:
        self._maybe_fail(name)
        if (schema, name) not in self.tables:
            raise ZonicError("DATA_REQUEST_INVALID", gateway_status=404, view=name)
        rows = [r for r in self.tables[(schema, name)] if all(_matches(r, f) for f in filters or [])]
        for order in reversed(order_by or []):
            present = [r for r in rows if r.get(order.column) is not None]
            missing = [r for r in rows if r.get(order.column) is None]
            present.sort(key=lambda r: r[order.column], reverse=order.descending)
            rows = present + missing  # NULLS LAST, as PostgreSQL does for ASC
        rows = rows[offset or 0 :]
        if limit is not None:
            rows = rows[:limit]
        if columns:
            rows = [{c: r.get(c) for c in columns} for r in rows]
        rows = copy.deepcopy(rows)
        return QueryResult(rows=rows, row_count=len(rows))

    # -- writes --------------------------------------------------------------

    async def procedure(
        self, datasource: str, schema: str, name: str, args: list[Any], *, retry: bool = False
    ) -> QueryResult:
        self.calls.append(Call("procedure", name, args))
        return await self._procedure(schema, name, args)

    async def _procedure(self, schema: str, name: str, args: list[Any]) -> QueryResult:
        self._maybe_fail(name)
        handler = self.procedures.get((schema, name))
        if handler is None:
            raise ZonicError("DATA_REQUEST_INVALID", gateway_status=404, procedure=name)
        rows = await handler(self, args)
        return QueryResult(rows=rows, row_count=len(rows))

    async def batch(self, items: list[BatchItem], *, mode: BatchMode, retry: bool = False) -> BatchResult:
        self.calls.append(Call(f"batch:{mode}", ",".join(i.payload.get("name", "") for i in items), items))
        snapshot = copy.deepcopy(self.tables) if mode == "transactional" else None

        async def run(item: BatchItem) -> BatchItemResult:
            p = item.payload
            try:
                if item.op == "view":
                    result = self._view(
                        p["schema"],
                        p["name"],
                        p.get("columns"),
                        [Filter.model_validate(f) for f in p.get("filters", [])],
                        [OrderBy.model_validate(o) for o in p.get("order_by", [])],
                        p.get("limit"),
                        p.get("offset"),
                    )
                else:
                    result = await self._procedure(p["schema"], p["name"], p.get("args") or [])
            except ZonicError as exc:
                return BatchItemResult(
                    id=item.id, status="error", error=BatchItemError(type=exc.code, message=exc.code)
                )
            return BatchItemResult(id=item.id, status="ok", rows=result.rows, row_count=result.row_count)

        if mode == "parallel":
            results = list(await asyncio.gather(*(run(i) for i in items)))
        else:
            results = []
            for item in items:
                outcome = await run(item)
                results.append(outcome)
                if outcome.status == "error" and mode == "transactional":
                    assert snapshot is not None
                    self.tables = snapshot
                    break
        errors = sum(1 for r in results if r.status == "error")
        return BatchResult(mode=mode, ok_count=len(results) - errors, error_count=errors, results=results)

    async def ready(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


def _like(pattern: str, value: str, flags: int = 0) -> bool:
    regex = "^" + "".join(".*" if ch == "%" else "." if ch == "_" else re.escape(ch) for ch in pattern) + "$"
    return re.match(regex, value, flags) is not None


def _matches(row: dict[str, Any], f: Filter) -> bool:
    value = row.get(f.column)
    match f.op:
        case FilterOp.EQ:
            return bool(value == f.value)
        case FilterOp.NE:
            return value is not None and value != f.value
        case FilterOp.LT:
            return value is not None and value < f.value
        case FilterOp.LE:
            return value is not None and value <= f.value
        case FilterOp.GT:
            return value is not None and value > f.value
        case FilterOp.GE:
            return value is not None and value >= f.value
        case FilterOp.IN:
            return value in (f.value or [])
        case FilterOp.NOT_IN:
            return value is not None and value not in (f.value or [])
        case FilterOp.LIKE:
            return isinstance(value, str) and _like(str(f.value), value)
        case FilterOp.ILIKE:
            return isinstance(value, str) and _like(str(f.value), value, re.IGNORECASE)
        case FilterOp.BETWEEN:
            low, high = f.value
            return value is not None and low <= value <= high
        case FilterOp.IS_NULL:
            return value is None
        case FilterOp.IS_NOT_NULL:
            return value is not None
    raise AssertionError(f.op)
