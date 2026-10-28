"""Canonical JSON and the revision hash (design 5.1 step 6).

Keys are sorted at every level and no whitespace is written, so equal documents give
equal text whatever order the builder saved their keys in.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def revision_hash(value: Any) -> str:
    """SHA-256 of the canonical JSON: a document revision, the blueprint and graph cache key."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
