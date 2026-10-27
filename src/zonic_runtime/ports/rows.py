"""Typed row reads, for core layers that may not import the gateway adapter (design 4.3).

``integrations.gateway.repository.GatewayRepository`` implements it; tests may pass a
repository over ``InMemoryGateway``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, TypeVar

from pydantic import BaseModel

from zonic_runtime.models.gateway.wire import Filter, OrderBy

R = TypeVar("R", bound=BaseModel)


class RowReader(Protocol):
    async def read(
        self,
        row_model: type[R],
        *,
        owner: str | None = None,
        filters: Sequence[Filter] | None = None,
        order_by: Sequence[OrderBy] | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[R]: ...
