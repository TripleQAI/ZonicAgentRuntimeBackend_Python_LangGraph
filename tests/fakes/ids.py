"""``SequentialIds``: deterministic identifiers for snapshot-stable tests (P0-11).

Each kind counts on its own: ``uuid("run")`` gives ``<prefix>-0000-4000-8000-000000000001``,
then ``...002``, where the prefix (8 hex characters of the kind's SHA-256) keeps kinds
apart, so ``run`` and ``session`` IDs never collide. ``trace_id`` and ``span_id`` follow the
OpenTelemetry and Langfuse widths (32 and 16 hexadecimal characters).
"""

from __future__ import annotations

import hashlib
import uuid as _uuid
from collections import Counter
from dataclasses import dataclass, field


@dataclass
class SequentialIds:
    _counts: Counter[str] = field(default_factory=Counter)

    def _next(self, kind: str) -> tuple[str, int]:
        self._counts[kind] += 1
        prefix = hashlib.sha256(kind.encode()).hexdigest()[:8]
        return prefix, self._counts[kind]

    def uuid(self, kind: str = "id") -> _uuid.UUID:
        prefix, n = self._next(kind)
        return _uuid.UUID(f"{prefix}-0000-4000-8000-{n:012x}")

    def trace_id(self) -> str:
        prefix, n = self._next("trace")
        return f"{prefix}{n:024x}"

    def span_id(self) -> str:
        prefix, n = self._next("span")
        return f"{prefix}{n:08x}"

    def count(self, kind: str) -> int:
        return self._counts[kind]
