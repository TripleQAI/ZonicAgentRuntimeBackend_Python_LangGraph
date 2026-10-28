"""The template resolver (design 5.3; plan P1-06), ported from the Node.js ``template-resolver.ts``.

``{{namespace.path}}``: a string that is exactly one token yields the typed value; embedded
tokens become text, objects and lists as JSON. There is no evaluation and no code
execution. Namespaces, aliases (``node.``, ``variable.``), the special roots
(``nodeOutput``, ``runtime.workflowMetaData``, ``approval.response``), bare names and node
path aliases come from ``resources/templates.json``; a node type's output aliases (``result``
to ``text``) come from its spec.

The resolver reads a ``TemplateScope``: plain namespace values, which the node template
builds from ``RunState`` (P1-08). It never writes; missing paths and bare names are
reported in ``TemplateIssues`` for the caller's missing-value policy.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from zonic_runtime.io.encoders import Encoder
from zonic_runtime.models.blueprint import Blueprint
from zonic_runtime.models.resources.manifest import NodeRuntimeManifest
from zonic_runtime.models.resources.misc import TemplateNamespaces

_EXACT = re.compile(r"^\s*\{\{\s*([^{}]+?)\s*\}\}\s*$")
_EMBEDDED = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
_ANY = re.compile(r"\{\{[^{}]+\}\}")
_INDEX = re.compile(r"^\d+$")

#: The node record field holding the output data (``nodes.X.output``).
OUTPUT_FIELD = "output"  # principle: allow rule-1-node-type node-record-field
#: Node record fields that are not output data.
NODE_META = frozenset({"status", "error", "visit", "usage", "name"})
#: The thread of ordinary chat messages (``{{messages}}``); others are ``{{thread.<name>}}``.
DEFAULT_THREAD = "messages"
#: Under this key, names match case-insensitively (platform documents write ``getglobalcode``).
_CASE_INSENSITIVE_PARENT = "toolResults"

# Keys of ``templates.json`` ``special``: what a special root reads.
CURRENT_NODE_OUTPUT = "current_node_output"
RUN_METADATA = "run_metadata"
INTERRUPT_ANSWER = "interrupt_answer"


@dataclass(frozen=True)
class TemplateScope:
    """The values templates may read. ``nodes`` holds node records by node ID."""

    system: Mapping[str, Any] = field(default_factory=dict)
    flow: Mapping[str, Any] = field(default_factory=dict)
    nodes: Mapping[str, Any] = field(default_factory=dict)
    history: Sequence[Any] = ()
    #: Messages of this run, each with ``thread`` (default ``messages``), ``role`` and ``content``.
    messages: Sequence[Mapping[str, Any]] = ()
    input: Mapping[str, Any] = field(default_factory=dict)
    run: Mapping[str, Any] = field(default_factory=dict)
    run_metadata: Mapping[str, Any] = field(default_factory=dict)
    interrupt_answer: Any = None

    def thread(self, name: str) -> list[Mapping[str, Any]]:
        return [m for m in self.messages if m.get("thread", DEFAULT_THREAD) == name]


@dataclass(frozen=True)
class TemplateNode:
    """A node a template can name (by name or ID), with its type's output aliases."""

    node_id: str
    aliases: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TemplateContext:
    scope: TemplateScope
    #: Name or ID -> node; the blueprint's name index (``Blueprint.resolve``).
    nodes: Callable[[str], TemplateNode | None] = lambda _: None
    #: The node holding the template: ``{{nodeOutput.*}}`` reads its output.
    node_id: str | None = None
    #: That node's output while it is being built (output structure and updates).
    node_output: Mapping[str, Any] | None = None


@dataclass
class TemplateIssues:
    """Paths that resolved to nothing, and bare names read from the bare-name namespace."""

    missing: list[str] = field(default_factory=list)
    bare: list[str] = field(default_factory=list)

    def _add(self, items: list[str], path: str) -> None:
        if path not in items:
            items.append(path)


def node_lookup(blueprint: Blueprint, manifest: NodeRuntimeManifest) -> Callable[[str], TemplateNode | None]:
    """``TemplateContext.nodes`` for a blueprint: name or ID through the name index, with the
    output aliases of each node's type (manifest ``io.output.aliases``)."""
    nodes: dict[str, TemplateNode] = {}
    for spec in blueprint.nodes.values():
        entry = manifest.node_types.get(spec.node_type)
        output = entry.io.output if entry is not None and entry.io is not None else None
        nodes[spec.node_id] = TemplateNode(spec.node_id, dict(output.aliases) if output is not None else {})

    def lookup(name_or_id: str) -> TemplateNode | None:
        spec = blueprint.resolve(name_or_id)
        return nodes.get(spec.node_id) if spec is not None else None

    return lookup


def has_template(value: Any) -> bool:
    return isinstance(value, str) and _ANY.search(value) is not None


def _to_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _plain(value: Any) -> Any:
    return value.model_dump(mode="json", by_alias=True) if isinstance(value, BaseModel) else value


def walk(value: Any, path: Sequence[str], parent: str = "") -> Any:
    """``None`` when the path is missing; list indexes are numbers."""
    cursor = _plain(value)
    parent_key = parent
    for key in path:
        if isinstance(cursor, list) and _INDEX.match(key):
            index = int(key)
            cursor = cursor[index] if index < len(cursor) else None
        elif isinstance(cursor, Mapping):
            if key in cursor:
                cursor = cursor[key]
            elif parent_key == _CASE_INSENSITIVE_PARENT:
                match = next((k for k in cursor if isinstance(k, str) and k.lower() == key.lower()), None)
                cursor = None if match is None else cursor[match]
            else:
                return None
        else:
            return None
        cursor = _plain(cursor)
        parent_key = key
    return cursor


class TemplateResolver:
    def __init__(self, config: TemplateNamespaces) -> None:
        self._config = config
        self._namespaces = set(config.namespaces)
        self._aliases = sorted(config.aliases.items(), key=lambda kv: -len(kv[0]))
        self._special = sorted(config.special.items(), key=lambda kv: -len(kv[0]))

    # -- public ---------------------------------------------------------------

    def resolve(
        self,
        value: Any,
        context: TemplateContext,
        issues: TemplateIssues | None = None,
        *,
        encode: Encoder | None = None,
    ) -> Any:
        """Templates in a value; dicts and lists are resolved item by item. ``encode`` rewrites
        each value embedded in a longer string (never a whole-string template), given the
        template's offset in the string: the HTTP kind encodes URL path and query values (X-03)."""
        issues = issues if issues is not None else TemplateIssues()
        if isinstance(value, str):
            return self._resolve_string(value, context, issues, encode)
        if isinstance(value, list):
            return [self.resolve(item, context, issues, encode=encode) for item in value]
        if isinstance(value, dict):
            return {key: self.resolve(item, context, issues, encode=encode) for key, item in value.items()}
        return value

    def read(self, path: str, context: TemplateContext, issues: TemplateIssues | None = None) -> Any:
        """The value at a template path (without braces); ``None`` when missing."""
        root, segments = self.canonical(path, issues)
        return self._lookup(root, segments, context)

    def canonical(self, path: str, issues: TemplateIssues | None = None) -> tuple[str, list[str]]:
        """``(namespace or special key, segments)`` with aliases applied and bare names placed.

        The static template check (P1-07) and the update writer use it too. A bare name under
        ``bareNames: error`` returns namespace ``""``, which reads nothing.
        """
        trimmed = path.strip()
        for root, key in self._special:
            if trimmed == root or trimmed.startswith(f"{root}."):
                return key, _split(trimmed[len(root) + 1 :])
        for alias, target in self._aliases:
            if trimmed == alias or trimmed.startswith(f"{alias}."):
                trimmed = f"{target}{trimmed[len(alias) :]}"
                break
        first, _, rest = trimmed.partition(".")
        if first in self._namespaces:
            return first, _split(rest)
        if issues is not None:
            issues._add(issues.bare, path.strip())
        if self._config.bare_names == "flow":
            return "flow", _split(trimmed)
        return "", _split(trimmed)

    # -- strings --------------------------------------------------------------

    def _resolve_string(
        self, value: str, context: TemplateContext, issues: TemplateIssues, encode: Encoder | None = None
    ) -> Any:
        exact = _EXACT.match(value)
        if exact:
            found = self.read(exact.group(1), context, issues)
            if found is None:
                issues._add(issues.missing, exact.group(1).strip())
            return found
        if "{{" not in value:
            return value

        def embed(match: re.Match[str]) -> str:
            found = self.read(match.group(1), context, issues)
            if found is None:
                issues._add(issues.missing, match.group(1).strip())
                return ""
            text = _to_text(found)
            return encode(match.start(), text) if encode is not None else text

        return _EMBEDDED.sub(embed, value)

    # -- lookup ---------------------------------------------------------------

    def _lookup(self, root: str, segments: list[str], context: TemplateContext) -> Any:
        scope = context.scope
        match root:
            case "nodes":
                return self._node(segments, context)
            case "flow":
                return walk(scope.flow, segments)
            case "system":
                return walk(scope.system, segments)
            case "history":
                return walk(list(scope.history), segments)
            case "messages":
                return walk(scope.thread(DEFAULT_THREAD), segments)
            case "thread":
                return walk(scope.thread(segments[0]), segments[1:]) if segments else None
            case "input":
                return walk(scope.input, segments)
            case "run":
                return walk(scope.run, segments)
            case "current_node_output":
                return self._current_output(segments, context)
            case "run_metadata":
                return walk(scope.run_metadata, segments)
            case "interrupt_answer":
                return walk(scope.interrupt_answer, segments)
        return None

    def _node(self, segments: list[str], context: TemplateContext) -> Any:
        if not segments:
            return None
        node = context.nodes(segments[0])
        if node is None:
            return None
        rest = segments[1:]
        first = rest[0] if rest else None
        # A node's own templates (output structure, updates) see the output being built.
        if node.node_id == context.node_id and context.node_output is not None and first not in NODE_META:
            return self._output_path(
                context.node_output, rest[1:] if first == OUTPUT_FIELD else rest, node.aliases
            )
        record = _plain(context.scope.nodes.get(node.node_id))
        if not isinstance(record, Mapping):
            return None
        if first is None:
            return dict(record)
        if first in NODE_META:
            return walk(record.get(first), rest[1:])
        output = record.get(OUTPUT_FIELD)
        return self._output_path(output, rest[1:] if first == OUTPUT_FIELD else rest, node.aliases)

    def _current_output(self, segments: list[str], context: TemplateContext) -> Any:
        data: Any = context.node_output
        if data is None and context.node_id is not None:
            record = _plain(context.scope.nodes.get(context.node_id))
            data = record.get(OUTPUT_FIELD) if isinstance(record, Mapping) else None
        node = context.nodes(context.node_id) if context.node_id is not None else None
        return self._output_path(data, segments, node.aliases if node else {})

    def _output_path(self, data: Any, path: Sequence[str], aliases: Mapping[str, str]) -> Any:
        """A path into a node's output. The first segment may be an output alias of the node's
        type (``result`` -> ``text``), or a node path alias meaning the whole output (script and
        tool ``result``) when the output has no such field."""
        data = _plain(data)
        if not path:
            return data
        first, rest = path[0], path[1:]
        if isinstance(data, Mapping) and first in data:
            return walk(data[first], rest, first)
        alias = aliases.get(first)
        if alias is not None and isinstance(data, Mapping):
            return walk(data.get(alias), rest, alias)
        if self._config.node_path_aliases.get(first) == OUTPUT_FIELD:
            return walk(data, rest)
        return walk(data, path)


def _split(path: str) -> list[str]:
    return [p for p in path.split(".") if p] if path else []
