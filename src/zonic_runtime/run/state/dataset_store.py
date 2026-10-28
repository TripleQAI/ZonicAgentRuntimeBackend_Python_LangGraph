"""The dataset store (design 8.2; plan P3-05), after the Node.js runtime's ``run/state/dataset-store.ts``.

The ``cache`` store writes through the cache service now; the ``database`` store adds body chunks
to the run's pending datasets, which the turn-end save sends with ``sp_dataset_save``. Both add
the metadata that ``sp_run_finish`` saves as chunk 0. Without a cache service, the ``cache`` store
falls back to the ``database`` store.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, timedelta
from typing import Any

from zonic_runtime.errors import ZonicError
from zonic_runtime.io.rows import columns_of, json_bytes, json_text
from zonic_runtime.models.engine import DatasetRef
from zonic_runtime.models.resources.settings import RuntimeSettings
from zonic_runtime.ports.datasets import (
    CacheStorePort,
    DatasetKind,
    DatasetOwner,
    DatasetStoreName,
    DatasetSummary,
)
from zonic_runtime.runtime.clock import Clock, SystemClock

UNAVAILABLE = "DATA_UNAVAILABLE"
_DAY_SECONDS = 86_400
_JSON = "application/json"


def chunks_of(kind: DatasetKind, value: Any, chunk_bytes: int) -> list[Any]:
    """The body chunks of a ``database``-store dataset, each about ``chunk_bytes`` of JSON: rows
    split into row slices, an HTTP response into slices of its body text (the first chunk carries
    its status and headers). ``DatasetService`` joins them back."""
    if kind == "rows" and isinstance(value, list):
        chunks: list[list[Any]] = []
        current: list[Any] = []
        size = 0
        for row in value:
            size_of = json_bytes(row)
            if current and size + size_of > chunk_bytes:
                chunks.append(current)
                current, size = [], 0
            current.append(row)
            size += size_of
        if current or not chunks:
            chunks.append(current)
        return list(chunks)
    if kind == "http_response" and isinstance(value, Mapping) and isinstance(value.get("body"), str):
        body: str = value["body"]
        head = {k: v for k, v in value.items() if k != "body"}
        parts = [body[at : at + chunk_bytes] for at in range(0, len(body), chunk_bytes)] or [""]
        return [{**head, "body": part} if i == 0 else {"body": part} for i, part in enumerate(parts)]
    return [value]


class DatasetStore:
    def __init__(
        self,
        settings: RuntimeSettings,
        cache: CacheStorePort | None = None,
        clock: Clock | None = None,
        new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self._settings, self._cache, self._new_id = settings, cache, new_id
        self._clock: Clock = clock or SystemClock()

    def store(self, preferred: DatasetStoreName | None = None) -> DatasetStoreName:
        wanted = preferred or self._settings.datasets.store
        return "database" if wanted == "cache" and self._cache is None else wanted

    async def keep(
        self,
        owner: DatasetOwner,
        kind: DatasetKind,
        value: Any,
        store: DatasetStoreName,
        content_type: str = _JSON,
    ) -> DatasetRef:
        dataset_id = self._new_id()
        target = self.store(store)
        expires_at = self._expiry(target)
        key = dataset_id
        if target == "cache" and self._cache is not None:
            key = f"zonic:dataset:{dataset_id}"
            await self._cache.put(key, value, self._settings.datasets.cache_ttl_seconds)
        else:
            for index, body in enumerate(chunks_of(kind, value, self._settings.datasets.chunk_bytes)):
                owner.pending.chunks.append(
                    {
                        "datasetId": dataset_id,
                        "chunkNo": index + 1,
                        "nodeId": owner.node_id,
                        "visit": owner.visit,
                        "kind": kind,
                        "contentType": content_type,
                        "body": body,
                        "expiresAt": expires_at,
                    }
                )
        return self._registered(owner, dataset_id, key, target, kind, content_type, value, expires_at)

    def register(self, owner: DatasetOwner, kind: DatasetKind, key: str) -> DatasetRef:
        for meta in owner.pending.metadata:
            if meta.get("cacheKey") == key:
                known = {k: v for k, v in meta.items() if k != "cacheKey"}
                return DatasetRef.model_validate({**known, "key": key, "columns": meta.get("columns") or []})
        return self._registered(owner, self._new_id(), key, "cache", kind, _JSON, None, self._expiry("cache"))

    def describe(
        self, owner: DatasetOwner, kind: DatasetKind, key: str, summary: DatasetSummary
    ) -> DatasetRef:
        expires_at = self._expiry("cache")
        return self._registered(owner, self._new_id(), key, "cache", kind, _JSON, None, expires_at, summary)

    async def adopt(self, owner: DatasetOwner, kind: DatasetKind, key: str) -> tuple[DatasetRef, Any]:
        if self._cache is None:
            raise ZonicError(UNAVAILABLE, reason="no cache service is configured", key=key)
        value = await self._cache.get(key)
        if value is None:
            raise ZonicError(UNAVAILABLE, reason="the cached result is gone", key=key)
        ref = self._registered(owner, self._new_id(), key, "cache", kind, _JSON, value, self._expiry("cache"))
        return ref, value

    def _registered(
        self,
        owner: DatasetOwner,
        dataset_id: str,
        key: str,
        store: DatasetStoreName,
        kind: DatasetKind,
        content_type: str,
        value: Any,
        expires_at: str,
        summary: DatasetSummary | None = None,
    ) -> DatasetRef:
        # A registered key's value is not read: its size and hash are the server's summary, else unknown.
        rows = value if kind == "rows" and isinstance(value, list) else None
        meta: dict[str, Any] = {
            "datasetId": dataset_id,
            "store": store,
            "key": key,
            "kind": kind,
            "contentType": content_type,
            "columns": columns_of(rows, self._settings.io.column_sample_rows)
            if rows is not None
            else list(summary.columns if summary else ()),
            "expiresAt": expires_at,
        }
        if rows is not None:
            meta["rowCount"] = len(rows)
        if value is not None:
            text = json_text(value)
            meta["sizeBytes"] = len(text.encode("utf-8"))
            meta["sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if summary is not None:
            meta |= {
                k: v
                for k, v in (
                    ("rowCount", summary.row_count),
                    ("sizeBytes", summary.size_bytes),
                    ("sha256", summary.sha256),
                )
                if v is not None
            }
        owner.pending.metadata.append(
            {
                **meta,
                "nodeId": owner.node_id,
                "visit": owner.visit,
                **({"cacheKey": key} if store == "cache" else {}),
            }
        )
        return DatasetRef.model_validate(meta)

    def _expiry(self, store: DatasetStoreName) -> str:
        seconds = (
            self._settings.datasets.cache_ttl_seconds
            if store == "cache"
            else self._settings.retention.datasets_days * _DAY_SECONDS
        )
        moment = self._clock.now().astimezone(UTC) + timedelta(seconds=seconds)
        return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")
