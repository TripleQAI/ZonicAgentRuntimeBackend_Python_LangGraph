"""The dataset ports (design 8.2, 9.3; plan P3-04, P3-05), after the Node.js runtime's
``ports/datasets.ts``.

The full data of database, HTTP and tool results is kept outside state, as a dataset: in the
``cache`` store (the cache service, ``ZONIC_CACHE_API_URL``) it is written now and outlives the
run by ``settings.datasets.cacheTtlSeconds``; in the ``database`` store its chunks are saved with
the turn. Either way its metadata is saved with the turn (``sp_run_finish`` chunk 0), so
``GET /v1/datasets/{datasetId}`` finds it for the run's owner, and only for them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from zonic_runtime.models.engine import DatasetRef

DatasetKind = Literal["rows", "http_response", "tool_result"]
DatasetStoreName = Literal["cache", "database"]


class CacheStorePort(Protocol):
    """The cache service: the store the Zonic MCP server writes ``saveToCache`` results to, and the
    runtime writes HTTP responses to (``PUT keys/<key>``). ``get`` answers ``None`` for a missing key;
    an unreachable service is ``DATA_UNAVAILABLE``."""

    async def get(self, key: str) -> Any: ...

    async def put(self, key: str, value: Any, ttl_seconds: int) -> None: ...


@dataclass(frozen=True)
class DatasetSummary:
    """What the MCP server reports about a value it cached (``result.preview``)."""

    row_count: int | None = None
    columns: tuple[str, ...] = ()
    size_bytes: int | None = None
    sha256: str | None = None


@dataclass
class PendingDatasets:
    """A run's datasets until the turn end: metadata rows for ``sp_run_finish`` and the body
    chunks of the ``database`` store for ``sp_dataset_save`` (both in wire shape)."""

    metadata: list[dict[str, Any]] = field(default_factory=list)
    chunks: list[dict[str, Any]] = field(default_factory=list)
    #: Full values small enough for templates (``settings.io.maxResolvedBytes``), by node ID.
    held: dict[str, Mapping[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class DatasetOwner:
    """The node visit a dataset belongs to, and where its pending rows go until the turn end."""

    run_id: str
    node_id: str
    visit: int
    pending: PendingDatasets


class DatasetPort(Protocol):
    def store(self, preferred: DatasetStoreName | None = None) -> DatasetStoreName:
        """The store data goes to: ``preferred``, else the settings'; ``database`` without a cache service."""
        ...

    async def keep(
        self,
        owner: DatasetOwner,
        kind: DatasetKind,
        value: Any,
        store: DatasetStoreName,
        content_type: str = "application/json",
    ) -> DatasetRef:
        """Keeps a value; returns its reference."""
        ...

    def register(self, owner: DatasetOwner, kind: DatasetKind, key: str) -> DatasetRef:
        """A value the MCP server cached under ``key``, not read (an agent tool's result)."""
        ...

    def describe(
        self, owner: DatasetOwner, kind: DatasetKind, key: str, summary: DatasetSummary
    ) -> DatasetRef:
        """A value the MCP server cached under ``key``, described by its summary (not read)."""
        ...

    async def adopt(self, owner: DatasetOwner, kind: DatasetKind, key: str) -> tuple[DatasetRef, Any]:
        """A value the MCP server cached under ``key``, read back: its reference and the value."""
        ...


@dataclass(frozen=True)
class RunDatasets:
    """The dataset port bound to one run (``RunPorts.datasets``): its pending datasets."""

    port: DatasetPort
    run_id: str
    pending: PendingDatasets = field(default_factory=PendingDatasets)

    def owner(self, node_id: str, visit: int) -> DatasetOwner:
        return DatasetOwner(self.run_id, node_id, visit, self.pending)
