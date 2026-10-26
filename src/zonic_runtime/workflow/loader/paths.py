"""Dotted paths into plain JSON objects (``config.settings.serverId``), for the dialect rules."""

from __future__ import annotations

from typing import Any

JsonObject = dict[str, Any]
_MISSING = object()


def _parts(path: str) -> list[str]:
    return [p for p in path.split(".") if p]


def get_path(item: Any, path: str, default: Any = None) -> Any:
    current = item
    for part in _parts(path):
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def has_path(item: Any, path: str) -> bool:
    return get_path(item, path, _MISSING) is not _MISSING


def set_path(item: JsonObject, path: str, value: Any) -> None:
    parts = _parts(path)
    current = item
    for part in parts[:-1]:
        nxt = current.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            current[part] = nxt
        current = nxt
    current[parts[-1]] = value


def delete_path(item: JsonObject, path: str) -> bool:
    parts = _parts(path)
    parent = get_path(item, ".".join(parts[:-1])) if len(parts) > 1 else item
    if isinstance(parent, dict) and parts and parts[-1] in parent:
        del parent[parts[-1]]
        return True
    return False


def json_type(value: Any) -> str:
    """JSON type name: ``object``, ``array``, ``string``, ``number``, ``boolean``, ``null``, ``undefined``."""
    if value is _MISSING:
        return "undefined"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def type_at(item: Any, path: str) -> str:
    return json_type(get_path(item, path, _MISSING))
