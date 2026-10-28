"""Recorded upstream answers (plan P3-12, case F-09), after the Node.js runtime's
``tests/fakes/recordings.ts``: the same file names, hashes and lookup, so both runtimes replay one
set of recordings (vendored from the Node.js repository by ``scripts/sync_recordings.py``).

A recording is one tool call: the parameter values it ran with and the upstream's answer, as the
MCP server returns it for one tool (``status``, ``output`` or ``error``), or an HTTP response.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
RECORDINGS_DIR = ROOT / "tests" / "fixtures" / "recordings"
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def _js(value: Any) -> Any:
    """Numbers as JavaScript writes them (``1.0`` is ``1``), so hashes match the Node.js ones."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return [_js(v) for v in value]
    if isinstance(value, Mapping):
        return {k: _js(v) for k, v in value.items()}
    return value


def canonical_json(value: Any) -> str:
    """The Node.js ``canonicalJson``: keys sorted at every level, no whitespace."""
    return json.dumps(_js(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def hash16(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()[:16]


def template_hash(config: Mapping[str, Any]) -> str:
    """An MCP tool's request template: its script, or its method, URL, query, headers and body.
    Two copies of a tool with one name (an agent's and a node's) may run different SQL."""
    return hash16(
        {
            key: config.get(key)
            for key in ("script", "method", "url", "queryParams", "headers", "bodyTemplate")
        }
    )


def _templates(document: Any) -> Iterator[str]:
    if isinstance(document, list):
        for item in document:
            yield from _templates(item)
    elif isinstance(document, Mapping):
        if document.get("toolType") in ("database", "api") and isinstance(document.get("name"), str):
            settings = document.get("settings")
            yield template_hash(settings if isinstance(settings, Mapping) else document)
        for item in document.values():
            yield from _templates(item)


def template_aliases(copy: Any, recorded: Any) -> dict[str, str]:
    """The templates of one document's database and API tools mapped to those of another with the
    same nodes (a workflow's SQL Server copy and its PostgreSQL copy), tool by tool in order."""
    to = list(_templates(recorded))
    return {template: to[i] if i < len(to) else template for i, template in enumerate(_templates(copy))}


def parameter_hash(parameters: Mapping[str, Any], template: str = "") -> str:
    return hash16(
        dict(parameters) if not template else {"parameters": dict(parameters), "template": template}
    )


def recording_file(workflow: str, tool: str, parameters: Mapping[str, Any], template: str = "") -> Path:
    return RECORDINGS_DIR / workflow / f"{_UNSAFE.sub('_', tool)}.{parameter_hash(parameters, template)}.json"


class Recordings:
    """The recordings of some workflows, looked up by tool name, parameter values and template."""

    def __init__(self, recordings: list[dict[str, Any]] | None = None) -> None:
        self._by_key: dict[str, dict[str, Any]] = {}
        for recording in recordings or []:
            self.add(recording)

    @classmethod
    def of(cls, *workflows: str) -> Recordings:
        found = []
        for workflow in workflows:
            folder = RECORDINGS_DIR / workflow
            if folder.is_dir():
                found += [json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.glob("*.json"))]
        return cls(found)

    def add(self, recording: dict[str, Any]) -> None:
        key = parameter_hash(recording["parameters"], recording.get("template") or "")
        self._by_key[f"{recording['tool']}|{key}"] = recording

    def find(self, tool: str, parameters: Mapping[str, Any], template: str = "") -> dict[str, Any] | None:
        return self._by_key.get(f"{tool}|{parameter_hash(parameters, template)}")

    def all(self) -> list[dict[str, Any]]:
        return list(self._by_key.values())

    def __len__(self) -> int:
        return len(self._by_key)
