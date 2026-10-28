"""The secret port (design 15, "Secrets"; plan P3-02, case T-04).

Documents hold only references, ``secret:<name>`` or ``{{secrets.<name>}}``; a literal credential
fails the document (``DOCUMENT_INVALID``, ``models/nodes/base.py``). ``resolve_secrets`` replaces
the references in a tool's configuration at call time, so a secret reaches the MCP server or the
egress guard and never state, events or observations. A missing secret is ``TOOL_FAILED``, naming
the reference, never a value.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from zonic_runtime.errors import ZonicError

_REFERENCE = re.compile(
    r"^(?:secret:(?P<plain>[A-Za-z0-9_./-]+)|\{\{\s*secrets\.(?P<template>[A-Za-z0-9_.-]+)\s*\}\})$"
)


@runtime_checkable
class SecretPort(Protocol):
    def get(self, name: str) -> str | None: ...


def secret_name(value: Any) -> str | None:
    found = _REFERENCE.match(value.strip()) if isinstance(value, str) else None
    return (found.group("plain") or found.group("template")) if found else None


def resolve_secrets(value: Any, get: Callable[[str], str | None]) -> Any:
    name = secret_name(value)
    if name is not None:
        secret = get(name)
        if secret is None:
            raise ZonicError("TOOL_FAILED", reason=f"secret {name} is not available")
        return secret
    if isinstance(value, list):
        return [resolve_secrets(item, get) for item in value]
    if isinstance(value, dict):
        return {key: resolve_secrets(item, get) for key, item in value.items()}
    return value
