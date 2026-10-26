"""The data-access port. Everything above ``integrations/gateway`` depends on this, not on pgfoundation."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from zonic_runtime.models.gateway.wire import BatchItem, BatchMode, BatchResult, Filter, OrderBy, QueryResult


@runtime_checkable
class GatewayPort(Protocol):
    """Datasources and schemas are logical keys (settings.gateway), never literal names."""

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
    ) -> QueryResult: ...

    async def procedure(
        self, datasource: str, schema: str, name: str, args: list[Any], *, retry: bool = False
    ) -> QueryResult: ...

    async def batch(self, items: list[BatchItem], *, mode: BatchMode, retry: bool = False) -> BatchResult: ...

    async def ready(self) -> bool: ...

    async def aclose(self) -> None: ...
