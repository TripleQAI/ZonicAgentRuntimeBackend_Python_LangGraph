"""Rows and inline previews (design 8.2), after the Node.js runtime's ``domain/rows.ts`` and
``previews.ts``: a node's output carries at most ``io.inlineRows`` rows and ``io.inlineBytes`` of
JSON; the full value stays in the dataset, and in the run for later templates while it fits
``io.maxResolvedBytes``. Sizes are of compact JSON in UTF-8, as JavaScript measures them.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def json_bytes(value: Any) -> int:
    return len(json_text(value).encode("utf-8"))


def columns_of(rows: Sequence[Any], sample: int) -> list[str]:
    """Every key of the first ``sample`` rows, in first-seen order."""
    columns: dict[str, None] = {}
    for row in rows[:sample]:
        if isinstance(row, Mapping):
            columns.update(dict.fromkeys(str(k) for k in row))
    return list(columns)


def preview[T](rows: Sequence[T], max_rows: int, max_bytes: int) -> list[T]:
    """The first rows of a result, within both limits."""
    kept: list[T] = []
    size = 2
    for row in rows[:max_rows]:
        size += json_bytes(row) + 1
        if size > max_bytes:
            break
        kept.append(row)
    return kept


def preview_text(text: str, max_bytes: int) -> str:
    """The first ``max_bytes`` of a text (UTF-8), cut at a character boundary."""
    data = text.encode("utf-8")
    return text if len(data) <= max_bytes else data[:max_bytes].decode("utf-8", errors="ignore")


def held_within(value: Any, max_bytes: int) -> bool:
    """Whether a value is small enough to keep in the run for templates."""
    return json_bytes(value) <= max_bytes


def rows_of(value: Any) -> list[dict[str, Any]]:
    """The rows of a database tool's output: a list of rows, or the last of a stored procedure's
    result sets."""
    if isinstance(value, list):
        return [dict(r) for r in value if isinstance(r, Mapping)]
    if isinstance(value, Mapping) and isinstance(value.get("resultSets"), list) and value["resultSets"]:
        return rows_of(value["resultSets"][-1])
    return []
