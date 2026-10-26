"""Workflow document models and fixtures (P1-01): cases M-04, F-01, F-02.

F-01's "each versioned copy gives the same blueprint" is completed by P1-05; F-02's
"an empty model resolves to the catalog default" by the normalizer (P1-03) and "template
IDs never appear in the catalog" by the loader (P1-04). Here: every document loads, the
version is read correctly, both copies load to the same document, templates unwrap, and
empty models are reported against a catalog that has a default.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.workflow_fixtures import (
    BUILDER,
    BUILDER_SCHEMA,
    SYNTHETIC,
    document_files,
    fixture_id,
    load_document,
    read_json,
)
from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import load_resources
from zonic_runtime.models.document import (
    DEFAULT_DOCUMENT_VERSION,
    WorkflowDocument,
    WorkflowEdge,
    WorkflowNode,
    WorkflowTemplate,
)

RESOURCES = load_resources(EnvironmentModel(env="dev"))
NODE_TYPES = set(RESOURCES.manifest.node_types)
FILES = document_files()


# --- F-01: every vendored document loads ---------------------------------------------


def test_F01_every_set_of_plan_20_1_is_vendored() -> None:
    counts = {
        folder: len(list((BUILDER / folder).glob("*.json")))
        for folder in ("zonic", "zonic-versioned", "templates", "invalid", "platform", "open-data", "django")
    }
    assert counts == {
        "zonic": 7,
        "zonic-versioned": 7,
        "templates": 4,
        "invalid": 1,
        "platform": 9,
        "open-data": 3,
        "django": 1,
    }
    assert len(list(SYNTHETIC.glob("*.json"))) == 12


@pytest.mark.parametrize("path", FILES, ids=[fixture_id(p) for p in FILES])
def test_F01_every_vendored_document_loads(path: Path) -> None:
    document = load_document(path)
    assert document.nodes, "a document without nodes"
    assert {n.type for n in document.nodes} <= NODE_TYPES
    ids = [n.id for n in document.nodes]
    assert len(ids) == len(set(ids)), "duplicate node IDs"


@pytest.mark.parametrize("path", sorted((BUILDER / "zonic").glob("*.json")), ids=lambda p: p.stem)
def test_F01_zonic_exports_read_as_1_0(path: Path) -> None:
    document = load_document(path)
    assert document.schema_version is None
    assert document.document_version == DEFAULT_DOCUMENT_VERSION == "1.0"


@pytest.mark.parametrize("path", sorted((BUILDER / "zonic").glob("*.json")), ids=lambda p: p.stem)
def test_F01_versioned_copy_loads_to_the_same_document(path: Path) -> None:
    plain = load_document(path)
    versioned = load_document(BUILDER / "zonic-versioned" / path.name)
    assert versioned.schema_version == "1.0"
    assert versioned.model_dump(exclude={"schema_version"}) == plain.model_dump(exclude={"schema_version"})


def test_F01_builder_2_0_documents() -> None:
    two = [fixture_id(p) for p in FILES if load_document(p).document_version == "2.0"]
    assert set(two) >= {
        "builder/templates/approval_flow",
        "builder/invalid/new-workflow",
        "builder/open-data/fsh-energy-agent",
        "synthetic/external-agent",
    }


def test_F01_every_node_type_has_a_fixture() -> None:
    covered = {n.type for p in FILES for n in load_document(p).nodes}
    assert covered == NODE_TYPES


def test_F01_unknown_keys_are_kept_and_reported() -> None:
    path = BUILDER / "platform" / "intelligent-buying-orchestrator.json"
    raw = read_json(path)
    document = load_document(path)
    report = document.unknown_key_report()
    assert {"createdBy", "originalBpc", "freezeRelease"} <= set(report)
    assert not [k for k in report if k.split(".")[-1] in {"selected", "dragging", "deletable"}]
    round_trip = document.model_dump(mode="json", by_alias=True, exclude_unset=True)
    assert round_trip["createdBy"] == raw["createdBy"]
    assert round_trip["nodes"][0].keys() >= raw["nodes"][0].keys() - {"selected", "dragging"}


def test_F01_both_variable_shapes() -> None:
    builder = load_document(BUILDER / "zonic" / "all-agents.json").variable_definitions()
    platform = load_document(BUILDER / "platform" / "quick-pay-assistant.json").variable_definitions()
    assert builder["allAgentsTestPoint"].default == "testing"
    assert platform["user_context"].type == "object" and platform["user_context"].ref_id == "user_context"


# --- F-02: templates ---------------------------------------------------------------


@pytest.mark.parametrize("path", sorted((BUILDER / "templates").glob("*.json")), ids=lambda p: p.stem)
def test_F02_template_document_member_loads(path: Path) -> None:
    template = WorkflowTemplate.model_validate(read_json(path))
    assert template.name and template.document.nodes
    assert template.document.agentic_workflow_id and template.document.agentic_workflow_id.startswith(
        "template-"
    )


@pytest.mark.parametrize("path", sorted((BUILDER / "templates").glob("*.json")), ids=lambda p: p.stem)
def test_F02_empty_models_are_reported_and_the_catalog_has_a_default(path: Path) -> None:
    document = load_document(path)
    empty = document.empty_model_nodes()
    assert empty, "the builder's templates leave the model empty"
    assert all(document.node(i) is not None for i in empty)
    assert RESOURCES.models.default_model in RESOURCES.models.models


def test_F02_saved_workflows_report_no_empty_model() -> None:
    assert load_document(BUILDER / "zonic" / "all-agents.json").empty_model_nodes() == []


# --- M-04: models against the builder schema ------------------------------------------

#: Model fields stricter than the schema on purpose would be listed here; none so far.
SCHEMA = read_json(BUILDER_SCHEMA)


def _types(prop: dict[str, Any], defs: dict[str, Any]) -> set[str]:
    """JSON types a (model or builder) property accepts, null excluded, refs resolved."""
    if "$ref" in prop:
        target = defs.get(prop["$ref"].rsplit("/", 1)[-1], {})
        return _types(target, defs) if target else {"object"}
    if "anyOf" in prop:
        return set().union(*(_types(p, defs) for p in prop["anyOf"]))
    kind = prop.get("type")
    if kind is None:
        return {"any"}
    kinds = {kind} if isinstance(kind, str) else set(kind)
    return kinds - {"null"}


def _check(builder: dict[str, Any], model_schema: dict[str, Any], where: str) -> list[str]:
    problems: list[str] = []
    model_defs = model_schema.get("$defs", {})
    model_props = model_schema["properties"]
    for name, prop in builder.get("properties", {}).items():
        if name not in model_props:
            problems.append(f"{where}.{name}: in the builder schema, not in the model")
            continue
        expected, actual = _types(prop, SCHEMA["$defs"]), _types(model_props[name], model_defs)
        if expected != {"any"} and "any" not in expected and not actual <= expected | {"integer"} & expected:
            problems.append(f"{where}.{name}: model accepts {sorted(actual)}, builder {sorted(expected)}")
        if "minLength" in prop and _min_length(model_props[name]) < prop["minLength"]:
            problems.append(
                f"{where}.{name}: model allows shorter strings than minLength {prop['minLength']}"
            )
        if "pattern" in prop and _pattern(model_props[name]) != prop["pattern"]:
            problems.append(f"{where}.{name}: pattern differs")
    missing_required = set(builder.get("required", [])) - set(model_schema.get("required", []))
    problems += [
        f"{where}.{r}: required by the builder, optional in the model" for r in sorted(missing_required)
    ]
    return problems


def _pattern(prop: dict[str, Any]) -> str | None:
    if "pattern" in prop:
        return str(prop["pattern"])
    return next((str(p["pattern"]) for p in prop.get("anyOf", []) if "pattern" in p), None)


def _min_length(prop: dict[str, Any]) -> int:
    return max([prop.get("minLength", 0)] + [p.get("minLength", 0) for p in prop.get("anyOf", [])])


def test_M04_document_models_agree_with_the_builder_schema() -> None:
    problems = _check(SCHEMA, WorkflowDocument.model_json_schema(by_alias=True), "document")
    problems += _check(SCHEMA["$defs"]["node"], WorkflowNode.model_json_schema(by_alias=True), "node")
    problems += _check(SCHEMA["$defs"]["edge"], WorkflowEdge.model_json_schema(by_alias=True), "edge")
    assert problems == []


def test_M04_node_types_agree_with_the_builder_schema() -> None:
    assert set(SCHEMA["$defs"]["nodeType"]["enum"]) == NODE_TYPES


def test_M04_every_builder_2_0_document_validates_against_both() -> None:
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(SCHEMA)
    builder_docs = [p for p in FILES if p.parent.name in {"synthetic", "open-data", "invalid"}]
    for path in builder_docs:
        assert not list(validator.iter_errors(read_json(path))), fixture_id(path)
        WorkflowDocument.model_validate(read_json(path))


def test_M04_model_is_never_looser_on_required_fields() -> None:
    with pytest.raises(ValueError):
        WorkflowNode.model_validate({"id": "", "type": "llm"})
    with pytest.raises(ValueError):
        WorkflowEdge.model_validate({"source": "a"})
    with pytest.raises(ValueError):
        WorkflowDocument.model_validate({"nodes": []})
    with pytest.raises(ValueError):
        WorkflowDocument.model_validate({"schemaVersion": "two", "nodes": [], "edges": []})
