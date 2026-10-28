"""The cache service in memory (P3-05): what the MCP server and the runtime share by key. ``down``
makes every call ``DATA_UNAVAILABLE``, as an unreachable service is."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from zonic_runtime.errors import ZonicError


@dataclass
class FakeCacheStore:
    values: dict[str, Any] = field(default_factory=dict)
    ttls: dict[str, int] = field(default_factory=dict)
    down: bool = False
    gets: list[str] = field(default_factory=list)

    async def get(self, key: str) -> Any:
        if self.down:
            raise ZonicError("DATA_UNAVAILABLE", reason="cache service unreachable")
        self.gets.append(key)
        return copy.deepcopy(self.values.get(key))

    async def put(self, key: str, value: Any, ttl_seconds: int) -> None:
        if self.down:
            raise ZonicError("DATA_UNAVAILABLE", reason="cache service unreachable")
        self.values[key] = copy.deepcopy(value)
        self.ttls[key] = ttl_seconds
