"""Start-up check of every manifest ``io`` block, and the node-type description (cases I-01, I-10).

Ported from the Node.js ``io-spec-validator.ts`` and ``node-type-catalog.ts``. Every
compiled node type declares its input and output. Each port has a known schema and a
source; each output schema and extension is known; each ``output.map`` key is a field of
the payload (or the payload is open) and each map path starts at ``$``; each alias names a
field. Any problem stops start-up, naming the node type and the field.
"""

from __future__ import annotations

from typing import Any

from zonic_runtime.config.settings import RuntimeResources
from zonic_runtime.errors import StartupError
from zonic_runtime.io.output_schemas import SPREAD
from zonic_runtime.io.schema_registry import JsonSchema, SchemaRegistry
from zonic_runtime.models.blueprint import IGNORED_KIND
from zonic_runtime.models.io.ports import SchemaRef
from zonic_runtime.models.resources.manifest import NodeRuntimeManifest


def _known(registry: SchemaRegistry, ref: SchemaRef) -> bool:
    return ref.inline is not None or (ref.ref is not None and registry.has(ref.ref))


def validate_io_specs(manifest: NodeRuntimeManifest, registry: SchemaRegistry) -> list[str]:
    problems: list[str] = []
    for node_type, entry in manifest.node_types.items():
        prefix = f"nodeTypes.{node_type}.io"
        if entry.io is None or entry.io.output is None:
            if entry.kind != IGNORED_KIND:
                problems.append(f"{prefix}: missing (every compiled node type declares its input and output)")
            continue
        for i, port in enumerate(entry.io.inputs):
            if not _known(registry, port.schema_ref):
                problems.append(f"{prefix}.inputs[{i}].schema: unknown schema {port.schema_ref.ref}")
        output = entry.io.output
        if not _known(registry, output.schema_ref):
            problems.append(f"{prefix}.output.schema: unknown schema {output.schema_ref.ref}")
            continue
        parts: list[JsonSchema] = [registry.schema(output.schema_ref)]
        for extension in output.extensions:
            if registry.has(extension):
                parts.append(registry.schema(extension))
            else:
                problems.append(f"{prefix}.output.extensions: unknown schema {extension}")
        is_open = any(p.get("additionalProperties") is True or not p.get("properties") for p in parts)
        fields = {name for p in parts for name in (p.get("properties") or {})}
        for field, path in output.map.items():
            if not path.startswith("$"):
                problems.append(f"{prefix}.output.map.{field}: path {path} must start at $")
            if field != SPREAD and not is_open and field not in fields:
                problems.append(f"{prefix}.output.map.{field}: not a field of {output.schema_ref.ref}")
        for alias, field in output.aliases.items():
            if not is_open and field not in fields:
                problems.append(f"{prefix}.output.aliases.{alias}: {field} is not a field of the payload")
    return problems


def build_schema_registry(resources: RuntimeResources) -> SchemaRegistry:
    """The process's schema registry; raises ``StartupError`` naming every bad ``io`` block."""
    registry = SchemaRegistry(resources.manifest, resources.builder, resources.settings.caches.validators)
    problems = validate_io_specs(resources.manifest, registry)
    if problems:
        raise StartupError("RESOURCE_INVALID", file="node-runtime.json", problems=problems)
    return registry


def describe_node_types(manifest: NodeRuntimeManifest, registry: SchemaRegistry) -> list[dict[str, Any]]:
    """The node types with their ports and output schemas, for ``GET /v1/meta/node-types``."""
    described = []
    for node_type, entry in manifest.node_types.items():
        if entry.io is None or entry.io.output is None:
            continue
        output = entry.io.output
        described.append(
            {
                "type": node_type,
                "kind": entry.kind,
                "inputs": [
                    {"name": p.name, "required": p.required, "schema": registry.schema(p.schema_ref)}
                    for p in entry.io.inputs
                ],
                "outputSchema": registry.compose(
                    [registry.schema(output.schema_ref), *(registry.schema(i) for i in output.extensions)]
                ),
                "outputAliases": dict(output.aliases),
            }
        )
    return described
