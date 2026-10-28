"""Payload schemas by ID and version (design 8.2; plan P1-07), ported from the Node.js
``schema-registry.ts`` with the ``jsonschema`` package in place of Ajv.

The registry holds the built-in ``@payload`` models, the manifest's ``schemaExtensions``
(port schemas and output extensions), and the builder's vendored ``outputSchema`` per node
type (``builder.<type>.output@1``). Pydantic's local ``$defs`` are inlined, so every
schema is self-contained and can be composed with ``allOf``. Validators are compiled once
and cached by the schema's canonical JSON, bounded by ``caches.validators``.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaError

from zonic_runtime.models.io.payloads import payload_models
from zonic_runtime.models.io.ports import SchemaRef
from zonic_runtime.models.resources.builder import BuilderRegistry
from zonic_runtime.models.resources.manifest import NodeRuntimeManifest
from zonic_runtime.runtime.lru import LruCache

JsonSchema = dict[str, Any]
_DEFS_PREFIX = "#/$defs/"
#: Payloads whose JSON Schema accepts any property.
OPEN_PAYLOADS = frozenset({"zonic.mapping@1", "zonic.run-result@1"})
_SCALARS = frozenset({"string", "number", "integer", "boolean", "null"})


@dataclass(frozen=True)
class SchemaIssue:
    """A validation failure: the field path and the problem."""

    path: str
    message: str


def _inline(schema: Any, defs: Mapping[str, Any]) -> Any:
    if isinstance(schema, list):
        return [_inline(item, defs) for item in schema]
    if not isinstance(schema, dict):
        return schema
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith(_DEFS_PREFIX):
        target = copy.deepcopy(defs[ref[len(_DEFS_PREFIX) :]])
        rest = {k: v for k, v in schema.items() if k != "$ref"}
        return _inline({**target, **rest}, defs)
    return {k: _inline(v, defs) for k, v in schema.items() if k != "$defs"}


def model_schema(model: Any) -> JsonSchema:
    """A model's JSON Schema with camelCase names and no ``$defs``."""
    raw = model.model_json_schema(by_alias=True)
    inlined: JsonSchema = _inline(raw, raw.get("$defs", {}))
    return inlined


def _path(error: SchemaError) -> str:
    parts = [f"[{p}]" if isinstance(p, int) else f".{p}" for p in error.absolute_path]
    if error.validator == "required" and isinstance(error.instance, dict):
        required = error.validator_value if isinstance(error.validator_value, list) else []
        missing = next((r for r in required if r not in error.instance), None)
        if missing is not None:
            parts.append(f".{missing}")
    return "".join(parts)


class SchemaRegistry:
    def __init__(self, manifest: NodeRuntimeManifest, builder: BuilderRegistry, max_validators: int) -> None:
        self._schemas: dict[str, JsonSchema] = {}
        for key, model in payload_models().items():
            schema = model_schema(model)
            # Payloads are open at the top level: outputs also carry output-structure fields and
            # parsed JSON answers; the static template check still knows the declared fields.
            schema.pop("additionalProperties", None)
            schema.pop("title", None)
            if key in OPEN_PAYLOADS:
                schema["additionalProperties"] = True
            self._schemas[key] = schema
        for key, extension in manifest.schema_extensions.items():
            self._schemas[key] = copy.deepcopy(extension)
        for node_type, entry in builder.node_types.items():
            self._schemas[self.builder_output_id(node_type)] = copy.deepcopy(entry.output_schema)
        self._validators: LruCache[str, Draft202012Validator] = LruCache(max_validators)

    @staticmethod
    def builder_output_id(node_type: str) -> str:
        return f"builder.{node_type.replace('_', '-')}.output@1"

    def has(self, schema_id: str) -> bool:
        return schema_id in self._schemas

    def ids(self) -> list[str]:
        return sorted(self._schemas)

    def schema(self, ref: SchemaRef | str) -> JsonSchema:
        """The JSON Schema of a registry ID or a ``SchemaRef`` (by ID or inline)."""
        if isinstance(ref, SchemaRef):
            if ref.inline is not None:
                return ref.inline
            ref = ref.ref or ""
        if ref not in self._schemas:
            raise KeyError(f"unknown schema {ref}")
        return self._schemas[ref]

    @staticmethod
    def compose(parts: Sequence[JsonSchema | None]) -> JsonSchema:
        """``allOf`` of the parts, absent and empty ones left out."""
        present = [p for p in parts if p]
        if not present:
            return {}
        return present[0] if len(present) == 1 else {"allOf": present}

    def validate(self, schema: JsonSchema, value: Any, base_path: str = "") -> list[SchemaIssue]:
        """Every issue with its field path under ``base_path``."""
        validator = self._validator(schema)
        return [
            SchemaIssue(path=f"{base_path}{_path(e)}", message=e.message)
            for e in sorted(validator.iter_errors(value), key=lambda e: list(e.absolute_path))
        ]

    def lookup(
        self, schema: JsonSchema, path: Sequence[str], case_insensitive_under: Collection[str] = ()
    ) -> JsonSchema | None:
        """The schema a path leads to, for the static template check: ``None`` when the path is
        unknown; ``{}`` when the schema is open at some level (anything may be there)."""
        return _walk(schema, list(path), frozenset(case_insensitive_under), "")

    def _validator(self, schema: JsonSchema) -> Draft202012Validator:
        key = json.dumps(schema, sort_keys=True, separators=(",", ":"))
        found = self._validators.get(key)
        if found is None:
            found = Draft202012Validator(schema)
            self._validators.put(key, found)
        return found


def _walk(schema: JsonSchema, path: list[str], insensitive: frozenset[str], parent: str) -> JsonSchema | None:
    if not path:
        return schema
    first, rest = path[0], path[1:]
    for key in ("allOf", "anyOf", "oneOf"):
        if isinstance(schema.get(key), list):
            is_open = False
            for part in schema[key]:
                # ``Optional`` fields are ``anyOf [schema, null]``; the null branch has no paths.
                if not isinstance(part, dict) or part.get("type") == "null":
                    continue
                found = _walk(part, path, insensitive, parent)
                if found:
                    return found
                if found is not None:
                    is_open = True
            if key == "allOf" or is_open:
                return {} if is_open else None
    if schema.get("type") == "array" and first.isdigit():
        items = schema.get("items")
        return _walk(items, rest, insensitive, first) if isinstance(items, dict) else {}
    properties = schema.get("properties")
    if isinstance(properties, dict) and properties:
        name = first if first in properties else None
        if name is None and parent in insensitive:
            name = next((k for k in properties if k.lower() == first.lower()), None)
        if name is not None and isinstance(properties[name], dict):
            return _walk(properties[name], rest, insensitive, name)
        extra = schema.get("additionalProperties")
        return {} if extra is True or isinstance(extra, dict) else None
    # A typed scalar has no paths below it; otherwise an open object or an untyped value.
    if schema.get("type") in _SCALARS:
        return None
    return {}
