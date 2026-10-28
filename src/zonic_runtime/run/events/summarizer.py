"""What an event may carry of a value (design Section 10; plan P2-01).

* ``summarize``: types and sizes only, never values (a node's output or a tool's input can
  hold row data and personal data; the full values are in the trace, masked).
* ``cap_data``: an event's data cut to ``settings.sse.maxEventBytes``; long strings are cut at
  a character boundary and large nested values are replaced by their cut JSON text.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

#: Marks cut text.
CUT = "…"
#: The smallest share of the cap a field keeps, in characters.
_MIN_SHARE = 16


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _bytes(value: Any) -> int:
    return len(_json(value).encode("utf-8"))


def summarize(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {"type": "object", "fields": len(value), "bytes": _bytes(value)}
    if isinstance(value, (list, tuple)):
        return {"type": "array", "length": len(value), "bytes": _bytes(value)}
    if isinstance(value, str):
        return {"type": "string", "length": len(value)}
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, (int, float)):
        return {"type": "number"}
    return {"type": type(value).__name__}


def cut_text(text: str, chars: int) -> str:
    """At most ``chars`` characters; Python strings index by code point, so no character is split."""
    return text if len(text) <= chars else text[: max(0, chars - 1)] + CUT


def cap_data(data: dict[str, Any], max_bytes: int) -> tuple[dict[str, Any], bool]:
    if _bytes(data) <= max_bytes:
        return data, False
    share = max(_MIN_SHARE, max_bytes // max(1, len(data)) // 4)
    capped: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, str):
            capped[key] = cut_text(value, share)
        elif isinstance(value, (Mapping, list, tuple)) and _bytes(value) > share * 4:
            capped[key] = cut_text(_json(value), share)
        else:
            capped[key] = value
    return capped, True
