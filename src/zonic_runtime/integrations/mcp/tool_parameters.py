"""The parameter values an MCP tool runs with, as the Zonic MCP server computes them (plan P3-12),
after the Node.js runtime's ``integrations/mcp/tool-parameters.ts``. Recordings are keyed by these
values, so the fakes and ``scripts/record_fixtures.py`` resolve them the same way as the server.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_TOKEN = re.compile(r"\{\{([^}]+)\}\}")
_WHOLE = re.compile(r"^\{\{([^}]+)\}\}$")
_FLOW, _SYSTEM = "flow.", "system."


def _at(source: Any, path: str) -> Any:
    current = source
    for part in path.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


def _js_string(value: Any) -> str:
    """``String(value)`` as JavaScript writes it, for values interpolated into text."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def resolve_server_tokens(value: Any, context: Mapping[str, Any]) -> Any:
    """The server's token resolution (``app/infra/resolver.py``): ``{{flow.*}}`` and
    ``{{system.*}}`` against the request context. A string that is one token becomes the value
    itself; tokens inside text are interpolated, and an unknown token stays as written."""
    if isinstance(value, list):
        return [resolve_server_tokens(item, context) for item in value]
    if isinstance(value, Mapping):
        return {key: resolve_server_tokens(item, context) for key, item in value.items()}
    if not isinstance(value, str):
        return value

    def token(name: str) -> Any:
        path = name.strip()
        if path.startswith(_FLOW):
            return _at(context.get("flow"), path.removeprefix(_FLOW))
        if path.startswith(_SYSTEM):
            return _at(context.get("system"), path.removeprefix(_SYSTEM))
        return None

    whole = _WHOLE.match(value.strip())
    if whole:
        return token(whole.group(1))

    def interpolate(match: re.Match[str]) -> str:
        found = token(match.group(1))
        return match.group(0) if found is None else _js_string(found)

    return _TOKEN.sub(interpolate, value)


def resolve_tool_parameters(tool: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any]:
    """Each declared parameter takes its input value, else its resolved ``sourcePath``, else its
    ``defaultValue``; input values without a declared parameter are kept (``app/infra/parameters.py``)."""
    given = tool.get("input") or {}
    config = tool.get("config") or {}
    declared = [p for p in config.get("parameters") or [] if isinstance(p, Mapping)]
    values: dict[str, Any] = {}
    for parameter in declared:
        name = parameter.get("name")
        if not isinstance(name, str) or not name:
            continue
        source_path = parameter.get("sourcePath")
        # A ``sourcePath`` whose token has no value falls back to the default, as on the server.
        source = (
            resolve_server_tokens(source_path, context)
            if isinstance(source_path, str) and source_path
            else None
        )
        if name in given:
            values[name] = given[name]
        elif source is not None:
            values[name] = source
        else:
            default = parameter.get("defaultValue")
            values[name] = default if default is not None else ""
    for name, value in given.items():
        values.setdefault(name, value)
    return values
