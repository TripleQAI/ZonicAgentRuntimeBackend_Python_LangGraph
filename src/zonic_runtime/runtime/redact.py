"""Redaction of personal data (plan P3-10; the ``mask``, ``guardrail`` and ``webhook`` hooks and the
stream), after the Node.js runtime's ``redact.ts`` and ``stream-mask.ts``.

Patterns are names of ``settings.hooks.patterns`` or regular expressions written ``/source/flags``
(the JavaScript notation both runtimes' settings use; ``g`` is implied, ``i``, ``m`` and ``s`` map
to Python's flags).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

DEFAULT_REPLACEMENT = "[REDACTED]"
_LITERAL = re.compile(r"^/(.+)/([a-z]*)$", re.DOTALL)
_FLAGS = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}


def _literal(text: str) -> re.Pattern[str] | None:
    found = _LITERAL.match(text)
    if found is None:
        return None
    flags = 0
    for flag in found.group(2):
        flags |= _FLAGS.get(flag, 0)
    return re.compile(found.group(1), flags)


def compile_patterns(patterns: Sequence[str], named: Mapping[str, str]) -> list[re.Pattern[str]]:
    """The expressions of pattern names and ``/source/flags`` texts; unknown names are skipped."""
    found: list[re.Pattern[str]] = []
    for pattern in patterns:
        compiled = _literal(named.get(pattern, pattern))
        if compiled is not None:
            found.append(compiled)
    return found


def unknown_patterns(patterns: Sequence[str], named: Mapping[str, str]) -> list[str]:
    """Patterns that are neither a known name nor a valid ``/source/flags`` expression."""
    unknown = []
    for pattern in patterns:
        try:
            if not compile_patterns([pattern], named):
                unknown.append(pattern)
        except re.error:
            unknown.append(pattern)
    return unknown


def redact(
    value: Any,
    named: Mapping[str, str],
    *,
    keys: Sequence[str] = (),
    patterns: Sequence[str] = (),
    replacement: str = DEFAULT_REPLACEMENT,
) -> tuple[Any, int]:
    """A copy with ``keys`` (at any depth, case-insensitive) and pattern matches replaced, and the
    number of replacements."""
    lowered = {k.lower() for k in keys}
    compiled = compile_patterns(patterns, named)
    count = 0

    def walk(item: Any) -> Any:
        nonlocal count
        if isinstance(item, str):
            for pattern in compiled:
                item, found = pattern.subn(replacement, item)
                count += found
            return item
        if isinstance(item, list):
            return [walk(child) for child in item]
        if isinstance(item, Mapping):
            out: dict[str, Any] = {}
            for key, child in item.items():
                if str(key).lower() in lowered:
                    count += 1
                    out[key] = replacement
                else:
                    out[key] = walk(child)
            return out
        return item

    return walk(value), count


@dataclass(frozen=True)
class StreamMaskRules:
    """What a node's ``mask`` bindings apply to its streamed text."""

    patterns: tuple[str, ...]
    replacement: str


class StreamMasker:
    """Masks text that arrives in pieces (a node's token deltas). ``push`` returns the text safe to
    stream: the last ``holdback`` characters stay buffered, and so does the start of any match that
    reaches into them, so a match split across pieces is masked whole. ``flush`` returns the rest.
    A match longer than ``holdback`` may stream in part."""

    def __init__(
        self, patterns: Sequence[str], named: Mapping[str, str], replacement: str, holdback: int
    ) -> None:
        self._patterns = compile_patterns(patterns, named)
        self._replacement = replacement
        self._holdback = holdback
        self._buffer = ""

    def push(self, text: str) -> str:
        self._buffer += text
        cut = max(0, len(self._buffer) - self._holdback)
        for pattern in self._patterns:
            for match in pattern.finditer(self._buffer):
                if match.start() < cut < match.end():
                    cut = match.start()
        ready, self._buffer = self._buffer[:cut], self._buffer[cut:]
        return self._mask(ready)

    def flush(self) -> str:
        rest, self._buffer = self._mask(self._buffer), ""
        return rest

    def _mask(self, text: str) -> str:
        for pattern in self._patterns:
            text = pattern.sub(self._replacement, text)
        return text
