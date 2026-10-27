"""Builds a node's ``NodeOutput`` from the kind's raw result (design 8.1 step 9; case I-05, I-07).

Ported from the Node.js ``output-mapper.ts``. The type's ``output.map`` selects fields from
the raw result (``$`` is the whole result, ``$.a.b`` a path into it; the key ``...`` spreads
an object into the output). The output starts from the payload model's defaults, so every
output of a type has one shape. With ``settings.io.outputValidation`` ``strict``, an output
failing the node's effective schema fails the node with ``OUTPUT_SCHEMA_INVALID``; with
``warn``, the issues are returned and the output is kept.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from typing import Any, Literal

from zonic_runtime.errors import ZonicError
from zonic_runtime.io.output_schemas import SPREAD
from zonic_runtime.io.schema_registry import JsonSchema, SchemaIssue, SchemaRegistry
from zonic_runtime.models.engine.records import DatasetRef, Usage
from zonic_runtime.models.io.node_io import NodeOutput
from zonic_runtime.models.io.payloads import payload_models
from zonic_runtime.models.io.ports import NodeIoSpec
from zonic_runtime.runtime.paths import get_path

#: Issue paths of an output start here, as templates name it (``nodes.X.output``).
OUTPUT_PATH = "output"  # principle: allow rule-1-node-type node-record-field


def _select(raw: Any, path: str) -> Any:
    if path == "$":
        return raw
    if path.startswith("$."):
        return get_path(raw, path[2:])
    return None


class OutputMapper:
    def __init__(self, schemas: SchemaRegistry, validation: Literal["strict", "warn"]) -> None:
        self._schemas = schemas
        self._validation = validation
        self._defaults: dict[str, dict[str, Any]] = {}

    def data(self, io: NodeIoSpec | None, raw: Any) -> dict[str, Any]:
        """The output data: the payload's defaults, mapped fields, then spread objects."""
        output = io.output if io is not None else None
        if output is None or not output.map:
            if isinstance(raw, dict):
                return dict(raw)
            return {} if raw is None else {"value": raw}
        data = self._payload_defaults(output.schema_ref.ref)
        for field, path in output.map.items():
            value = _select(raw, path)
            if field == SPREAD:
                if isinstance(value, dict):
                    for key, item in value.items():
                        data.setdefault(key, item)
            elif value is not None:
                data[field] = value
        return data

    def build(
        self,
        node_id: str,
        schema: JsonSchema | None,
        data: dict[str, Any],
        *,
        parts: Sequence[dict[str, Any]] = (),
        usage: Usage | None = None,
        refs: Sequence[DatasetRef] = (),
    ) -> tuple[NodeOutput, list[SchemaIssue]]:
        """Validates ``data`` against the node's effective schema; returns the envelope and the
        issues kept under ``warn``."""
        issues = self._schemas.validate(schema, data, OUTPUT_PATH) if schema else []
        if issues and self._validation == "strict":
            raise ZonicError(
                "OUTPUT_SCHEMA_INVALID",
                node_id=node_id,
                issues=[{"path": i.path, "message": i.message} for i in issues],
            )
        return NodeOutput(data=data, parts=list(parts), refs=list(refs), usage=usage), issues

    def _payload_defaults(self, schema_id: str | None) -> dict[str, Any]:
        if schema_id is None:
            return {}
        if schema_id not in self._defaults:
            model = payload_models().get(schema_id)
            self._defaults[schema_id] = (
                model().model_dump(mode="json", by_alias=True, exclude_none=True) if model is not None else {}
            )
        return copy.deepcopy(self._defaults[schema_id])
