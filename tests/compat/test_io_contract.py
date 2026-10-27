"""The runtime payloads against the builder's output schemas, and the vendored templates (P1-07: I-02, I-09)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from tests.workflow_fixtures import document_files, fixture_id, read_json
from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import load_resources
from zonic_runtime.errors import ZonicError
from zonic_runtime.io.output_schemas import known_output_schema
from zonic_runtime.io.specs import build_schema_registry
from zonic_runtime.workflow.blueprint.builder import BlueprintBuilder
from zonic_runtime.workflow.loader.loader import WorkflowLoader

DEV = load_resources(EnvironmentModel(env="dev"))
REGISTRY = build_schema_registry(DEV)
#: A runtime type may be the builder's type, or narrower.
COMPATIBLE = {"number": {"number", "integer"}}


def _types(schema: dict[str, Any]) -> set[str]:
    """The JSON types a field schema allows; empty when it is untyped (any value)."""
    if isinstance(schema.get("type"), str):
        return {schema["type"]}
    found: set[str] = set()
    for part in schema.get("anyOf") or schema.get("oneOf") or []:
        if isinstance(part, dict) and part.get("type") not in (None, "null"):
            found |= _types(part)
    return found


@pytest.mark.parametrize("node_type", sorted(DEV.builder.node_types))
def test_I02_the_runtime_payload_is_a_superset_of_the_builder_output_schema(node_type: str) -> None:
    builder = DEV.builder.node_types[node_type].output_schema
    entry = DEV.manifest.node_types[node_type]
    if not builder.get("properties"):
        return
    known = known_output_schema(entry.io, {}, REGISTRY)
    assert known is not None, node_type
    runtime = known.get("properties", {})
    for field, schema in builder["properties"].items():
        assert field in runtime, f"{node_type}: builder field {field} is not in the runtime payload"
        expected = schema.get("type")
        found = _types(runtime[field])
        if expected and found:
            assert found <= COMPATIBLE.get(expected, {expected}), (node_type, field, expected, found)


def test_I09_the_vendored_documents_have_only_the_known_stale_reference() -> None:
    features = DEV.settings.features.model_copy(update={"script_node": True})
    resources = replace(DEV, settings=DEV.settings.model_copy(update={"features": features}))
    loader, builder = WorkflowLoader.from_resources(resources), BlueprintBuilder(resources, REGISTRY)
    unknown = []
    for path in document_files():
        try:
            blueprint = builder.build(loader.from_stored(read_json(path), fixture_id(path)))
        except ZonicError:
            continue  # the invalid fixtures, covered elsewhere
        unknown += [
            (fixture_id(path), w.details["template"])
            for w in blueprint.warnings
            if w.code == "TEMPLATE_PATH_UNKNOWN"
        ]
    assert unknown == [
        (
            "builder/platform/fast-track-questionnaire-assistant",
            "nodeOutput.toolResults.fast_track_buying_new_questionnaire.text",
        )
    ]
