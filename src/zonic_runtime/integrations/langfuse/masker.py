"""Masking and capture rules of observations (design 12; plan P2-07, cases L-10, X-06).

``observability.json`` decides, per node type, whether a node's input and output are captured
``full`` (masked), as a ``summary`` (types and sizes only, so row data stays out) or not at all
(``none``: human input answers). Masking replaces the values of sensitive keys (case-insensitive
substrings: ``password``, ``token``, ``apikey`` ...) and every match of the configured patterns
(e-mail addresses, card numbers, bearer tokens) with the replacement, and cuts long strings to
``limits.maxFieldBytes``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from zonic_runtime.models.resources.manifest import Capture
from zonic_runtime.models.resources.misc import ObservabilityConfig

CUT = "…"
_FULL, _SUMMARY = "full", "summary"


def summary(value: Any) -> Any:
    """Types and sizes only."""
    if isinstance(value, Mapping):
        return {"type": "object", "fields": len(value), "keys": sorted(str(k) for k in value)[:20]}
    if isinstance(value, (list, tuple)):
        return {"type": "array", "length": len(value)}
    if isinstance(value, str):
        return {"type": "string", "length": len(value)}
    return {"type": "null" if value is None else type(value).__name__}


class Masker:
    def __init__(self, config: ObservabilityConfig) -> None:
        self._config = config
        self._keys = tuple(k.lower() for k in config.mask.keys)
        self._patterns = [re.compile(p) for p in config.mask.patterns.values()]
        self._replacement = config.mask.replacement
        self._max_bytes = config.limits.max_field_bytes

    def _sensitive(self, key: Any) -> bool:
        name = str(key).lower().replace("-", "").replace("_", "")
        return any(k.replace("_", "") in name for k in self._keys)

    def text(self, text: str) -> str:
        for pattern in self._patterns:
            text = pattern.sub(self._replacement, text)
        encoded = text.encode("utf-8")
        if len(encoded) > self._max_bytes:
            text = encoded[: self._max_bytes].decode("utf-8", errors="ignore") + CUT
        return text

    def mask(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {k: self._replacement if self._sensitive(k) else self.mask(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.mask(v) for v in value]
        if isinstance(value, str):
            stripped = value.strip()
            if stripped[:1] in ("{", "[") and len(stripped) < 4 * self._max_bytes:
                try:
                    return json.dumps(self.mask(json.loads(stripped)), ensure_ascii=False)
                except ValueError:
                    pass
            return self.text(value)
        return value

    def __call__(self, *, data: Any, **_: Any) -> Any:
        """Langfuse's ``mask``: every input and output of every observation, before export."""
        return self.mask(data)

    def capture(self, node_type: str | None, part: str, fallback: Capture | None = None) -> Capture:
        rule = self._config.capture_by_node_type.get(node_type or "")
        found: Capture | None = getattr(rule, part, None) if rule is not None else None
        return found or fallback or "summary"

    def captured(self, value: Any, mode: str) -> Any:
        if mode == _FULL:
            return self.mask(value)
        if mode == _SUMMARY:
            return summary(value)
        return None
