"""The static template check of a blueprint (design 8.2; plan P1-07, case I-09).

Ported from the Node.js ``template-checker.ts``; it lives with the blueprint rather than in
``io`` because it reads the template resolver's canonical paths (design 4.3). Every
``{{nodes.X.output.path}}``, ``{{nodes.X.result.path}}``, ``{{nodes.X.<field>}}`` and
``{{nodeOutput.path}}`` in a node's configuration is checked against node X's known output
schema before the run starts. An unknown path or node is ``TEMPLATE_PATH_UNKNOWN``, a warning
or an error by ``settings.io.templatePathUnknown``; a bare name is an ``info`` finding.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping
from typing import Any, Literal

from zonic_runtime.io.schema_registry import JsonSchema, SchemaRegistry
from zonic_runtime.models.blueprint import BlueprintWarning, NodeSpec
from zonic_runtime.models.resources.misc import TemplateNamespaces
from zonic_runtime.workflow.templates.resolver import (
    CURRENT_NODE_OUTPUT,
    NODE_META,
    OUTPUT_FIELD,
    TemplateIssues,
    TemplateResolver,
)

_TOKEN = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
_CASE_INSENSITIVE = frozenset({"toolResults"})
PATH_UNKNOWN = "TEMPLATE_PATH_UNKNOWN"
BARE_NAME = "TEMPLATE_BARE_NAME"


def templates_in(value: Any, path: str) -> Iterator[tuple[str, str]]:
    """``(configuration path, template path)`` for every token in a value."""
    if isinstance(value, str):
        for match in _TOKEN.finditer(value):
            yield path, match.group(1)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from templates_in(item, f"{path}[{i}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from templates_in(item, f"{path}.{key}")


class TemplateChecker:
    def __init__(
        self,
        schemas: SchemaRegistry,
        resolver: TemplateResolver,
        templates: TemplateNamespaces,
        severity: Literal["warning", "error"],
    ) -> None:
        self._schemas = schemas
        self._resolver = resolver
        self._templates = templates
        self._severity = severity

    def check(
        self,
        specs: Mapping[str, NodeSpec],
        configs: Mapping[str, dict[str, Any]],
        known: Mapping[str, JsonSchema | None],
        resolve_node: Callable[[str], NodeSpec | None],
    ) -> list[BlueprintWarning]:
        findings: list[BlueprintWarning] = []
        for node_id, spec in specs.items():
            for path, template in templates_in(configs.get(node_id, {}), f"nodes[{node_id}].config"):
                issues = TemplateIssues()
                self._resolver.canonical(template, issues)
                for bare in issues.bare:
                    findings.append(
                        BlueprintWarning(
                            code=BARE_NAME,
                            severity="info",
                            message=f"bare name {{{{{bare}}}}} reads {self._templates.bare_names}",
                            node_id=node_id,
                            details={"path": path},
                        )
                    )
                problem = self._problem(template, spec, known, resolve_node)
                if problem is not None:
                    findings.append(
                        BlueprintWarning(
                            code=PATH_UNKNOWN,
                            severity=self._severity,
                            message=problem,
                            node_id=node_id,
                            details={"path": path, "template": template},
                        )
                    )
        return findings

    def _problem(
        self,
        template: str,
        holder: NodeSpec,
        known: Mapping[str, JsonSchema | None],
        resolve_node: Callable[[str], NodeSpec | None],
    ) -> str | None:
        root, segments = self._resolver.canonical(template)
        if root == CURRENT_NODE_OUTPUT:
            # ``{{nodeOutput.*}}`` reads the output of the node holding the template.
            target, field, rest = holder, OUTPUT_FIELD, segments
        elif root == "nodes":
            if not segments:
                return f"template {{{{{template}}}}} names no node"
            found = resolve_node(segments[0])
            if found is None:
                return (
                    f"template {{{{{template}}}}} names node {segments[0]}, which the document does not have"
                )
            if len(segments) == 1:
                return None
            target, field, rest = found, segments[1], segments[2:]
        else:
            return None
        if field in NODE_META:
            return None
        schema = known.get(target.node_id)
        if schema is None:
            return None
        path = rest if field == OUTPUT_FIELD else [field, *rest]
        if not path:
            return None
        declared = path[0] in (schema.get("properties") or {})
        output = target.io.output if target.io is not None else None
        alias = output.aliases.get(path[0]) if output is not None else None
        if not declared and alias is not None:
            path = [alias, *path[1:]]
        elif not declared and self._templates.node_path_aliases.get(path[0]) == OUTPUT_FIELD:
            path = path[1:]
        if not path:
            return None
        if self._schemas.lookup(schema, path, _CASE_INSENSITIVE) is None:
            return (
                f"template {{{{{template}}}}} reads {'.'.join(path)}, which is not in the output schema"
                f" of {target.name}"
            )
        return None
