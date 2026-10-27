"""Effective and known output schemas of a node (design 8.2; plan P1-07, cases I-06, I-09).

Ported from the Node.js ``output-schemas.ts``. The effective schema validates a node's
output: ``allOf(payload, manifest extensions, the node's own outputSchema)``. The builder's
vendored ``outputSchema`` is not composed in: the runtime payload is held to be a superset of
it by a contract test (case I-02), and composing it would narrow fields the builder types
loosely (a Human Input ``value`` is a string there, an object for form answers here).

The known schema is what the static template check may read: the declared fields of those
parts, the output map's targets, output-structure targets, structured output, and the names
of the node's tools under ``toolResults``.
"""

from __future__ import annotations

import copy
from typing import Any

from zonic_runtime.io.schema_registry import JsonSchema, SchemaRegistry
from zonic_runtime.models.io.ports import NodeIoSpec

#: The spread key of an output map: the object at the path is merged into the output data.
SPREAD = "..."


def _node_schema(io: NodeIoSpec, config: dict[str, Any]) -> JsonSchema | None:
    own = config.get("outputSchema")
    if io.output is not None and io.output.node_schema_extends and isinstance(own, dict) and own:
        return own
    return None


def effective_output_schema(
    io: NodeIoSpec | None, config: dict[str, Any], registry: SchemaRegistry
) -> JsonSchema | None:
    if io is None or io.output is None:
        return None
    output = io.output
    return registry.compose(
        [
            registry.schema(output.schema_ref),
            *(registry.schema(i) for i in output.extensions),
            _node_schema(io, config),
        ]
    )


def _open(schema: JsonSchema) -> bool:
    extra = schema.get("additionalProperties")
    return extra is True or isinstance(extra, dict)


def _merge_known(target: JsonSchema, part: JsonSchema) -> None:
    """Declared properties of a schema part; a level with declared properties stays closed
    unless a part opens it."""
    for key in ("allOf", "anyOf", "oneOf"):
        if isinstance(part.get(key), list):
            for item in part[key]:
                if isinstance(item, dict) and item.get("type") != "null":
                    _merge_known(target, item)
    if _open(part):
        target["additionalProperties"] = True
    properties = part.get("properties")
    if not isinstance(properties, dict):
        return
    known = target.setdefault("properties", {})
    for name, schema in properties.items():
        if not isinstance(schema, dict):
            continue
        if isinstance(known.get(name), dict):
            _merge_known(known[name], schema)
        else:
            shell = {
                k: copy.deepcopy(v)
                for k, v in schema.items()
                if k not in ("properties", "allOf", "anyOf", "oneOf")
            }
            known[name] = shell
            _merge_known(shell, schema)


def known_output_schema(
    io: NodeIoSpec | None, config: dict[str, Any], registry: SchemaRegistry
) -> JsonSchema | None:
    if io is None or io.output is None:
        return None
    output = io.output
    known: JsonSchema = {"type": "object"}
    base = registry.schema(output.schema_ref)
    if not base.get("properties") or _open(base) or SPREAD in output.map:
        known["additionalProperties"] = True
    for part in (base, *(registry.schema(i) for i in output.extensions), _node_schema(io, config)):
        if part:
            _merge_known(known, part)
    fields: JsonSchema = {target: {} for target in output.map if target != SPREAD}
    for entry in config.get("outputStructure") or []:
        if isinstance(entry, dict) and isinstance(entry.get("target"), str):
            fields[entry["target"]] = {}
    structured = config.get("structuredOutput")
    if isinstance(structured, dict) and isinstance(structured.get("schema"), dict):
        fields["structuredOutput"] = structured["schema"]
    _merge_known(known, {"properties": fields})
    tools = [
        t for t in [*(config.get("tools") or []), *(config.get("routingTools") or [])] if isinstance(t, dict)
    ]
    properties = known.get("properties", {})
    if tools and "toolResults" in properties:
        names: JsonSchema = {}
        for tool in tools:
            settings = tool.get("settings")
            settings_alias = settings.get("alias") if isinstance(settings, dict) else None
            for name in (tool.get("name"), tool.get("alias"), settings_alias):
                if isinstance(name, str) and name:
                    names[name] = {}
        properties["toolResults"] = {"type": "object", "properties": names}
    return known
