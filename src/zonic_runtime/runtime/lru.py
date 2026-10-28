"""A bounded least-recently-used map for the per-process caches (design 6.8: documents,
blueprints, graphs). Not thread-safe: the caches live on the event loop."""

from __future__ import annotations

from collections import OrderedDict


class LruCache[K, V]:
    def __init__(self, max_entries: int) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._max = max_entries
        self._items: OrderedDict[K, V] = OrderedDict()

    def get(self, key: K) -> V | None:
        if key not in self._items:
            return None
        self._items.move_to_end(key)
        return self._items[key]

    def put(self, key: K, value: V) -> None:
        self._items[key] = value
        self._items.move_to_end(key)
        while len(self._items) > self._max:
            self._items.popitem(last=False)

    def pop(self, key: K) -> V | None:
        return self._items.pop(key, None)

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, key: object) -> bool:
        return key in self._items
