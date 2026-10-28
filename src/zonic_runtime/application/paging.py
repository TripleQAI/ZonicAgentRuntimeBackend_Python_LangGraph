"""Opaque cursors and page sizes of the list endpoints (ported from the Node.js runtime).

A cursor is base64url JSON of the last item's sort key; a cursor a client altered is
``INPUT_INVALID``.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from typing import Any

from zonic_runtime.errors import ZonicError
from zonic_runtime.models.resources.settings import ApiSettings


def invalid(path: str, message: str) -> ZonicError:
    return ZonicError("INPUT_INVALID", issues=[{"path": path, "message": message}])


def encode_cursor(key: Mapping[str, Any]) -> str:
    return (
        base64.urlsafe_b64encode(json.dumps(dict(key), separators=(",", ":")).encode()).decode().rstrip("=")
    )


def decode_cursor(cursor: str, fields: Mapping[str, type]) -> dict[str, Any]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise invalid("cursor", "is not a valid cursor") from exc
    if not isinstance(value, dict) or any(not isinstance(value.get(f), t) for f, t in fields.items()):
        raise invalid("cursor", "is not a valid cursor")
    return value


def page_size(limit: int | None, settings: ApiSettings) -> int:
    if limit is None:
        return settings.default_page_size
    if not 1 <= limit <= settings.max_page_size:
        raise invalid("limit", f"must be between 1 and {settings.max_page_size}")
    return limit


def contains_pattern(text: str) -> str:
    """A ``like`` pattern matching ``text`` literally anywhere (PostgreSQL escapes with a backslash)."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"
