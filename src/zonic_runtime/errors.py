"""The runtime's one exception type.

Every failure the runtime raises on purpose is a ``ZonicError`` with a code from
``resources/error-catalog.json``. The catalog decides the HTTP status, the user
message and whether the code is retryable; code raising the error decides only
the code and the details. This module imports nothing from the package so that
every layer can use it.
"""

from __future__ import annotations

from typing import Any


class ZonicError(Exception):
    """A failure with a catalog code. ``details`` holds paths and values, never prose."""

    def __init__(self, code: str, /, **details: Any) -> None:
        super().__init__(code)
        self.code = code
        self.details: dict[str, Any] = details

    def __repr__(self) -> str:
        return f"ZonicError({self.code!r}, {self.details!r})"


class StartupError(ZonicError):
    """Invalid environment or resource file; the process exits with one message."""
