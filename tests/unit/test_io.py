"""Node input and output contract (P1-07: I-01, I-03 to I-11, B-10)."""

from __future__ import annotations

import copy
from dataclasses import replace
from itertools import pairwise
from typing import Any

import pytest

from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import RuntimeResources, load_resources
from zonic_runtime.errors import StartupError, ZonicError
from zonic_runtime.io.output_mapper import OutputMapper
from zonic_runtime.io.port_binder import PortBinder
from zonic_runtime.io.schema_registry import SchemaRegistry
from zonic_runtime.io.specs import build_schema_registry, describe_node_types, validate_io_specs
from zonic_runtime.models.base import ZonicModel
from zonic_runtime.models.blueprint import Blueprint
from zonic_runtime.models.dialects import OutputMappingEntry
from zonic_runtime.models.io.payloads import payload, payload_models
from zonic_runtime.models.resources.manifest import NodeRuntimeManifest
from zonic_runtime.workflow.blueprint.builder import BlueprintBuilder
from zonic_runtime.workflow.loader.loader import WorkflowLoader
from zonic_runtime.workflow.templates.resolver import TemplateContext, TemplateResolver, TemplateScope
from zonic_runtime.workflow.templates.updates import build_mapping

DEV = load_resources(EnvironmentModel(env="dev"))
REGISTRY = build_schema_registry(DEV)
RESOLVER = TemplateResolver(DEV.templates)


def manifest_with(change: Any) -> NodeRuntimeManifest:
    data = DEV.manifest.model_dump(by_alias=True, exclude_none=True)
    change(data)
    return NodeRuntimeManifest.model_validate(data)


def resources_with(manifest: NodeRuntimeManifest | None = None, **settings: Any) -> RuntimeResources:
    found = replace(DEV, manifest=manifest or DEV.manifest)
    if settings:
        io = found.settings.io.model_copy(update=settings)
        found = replace(found, settings=found.settings.model_copy(update={"io": io}))
    return found


def build(doc: dict[str, Any], resources: RuntimeResources = DEV) -> Blueprint:
    return BlueprintBuilder(resources).build(WorkflowLoader.from_resources(resources).from_stored(doc, "wf"))


def doc(*nodes: dict[str, Any]) -> dict[str, Any]:
    chain = [{"id": "start", "type": "start"}, *nodes]
    return {
        "schemaVersion": "2.0",
        "nodes": chain,
        "edges": [{"source": a["id"], "target": b["id"]} for a, b in pairwise(chain)],
    }


# --- I-01: start-up check ------------------------------------------------------------------


def test_I01_every_manifest_entry_is_valid_at_start_up() -> None:
    assert validate_io_specs(DEV.manifest, REGISTRY) == []
    compiled = {t for t, e in DEV.manifest.node_types.items() if e.kind != "ignore"}
    assert {t for t, e in DEV.manifest.node_types.items() if e.io is not None} == compiled


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (
            lambda d: d["nodeTypes"]["llm"]["io"]["inputs"][0]["schema"].update(ref="port.nope@1"),
            "nodeTypes.llm.io.inputs[0].schema: unknown schema port.nope@1",
        ),
        (
            lambda d: d["nodeTypes"]["llm"]["io"]["output"]["map"].update(bogus="$.x"),
            "nodeTypes.llm.io.output.map.bogus: not a field of zonic.text@1",
        ),
        (
            lambda d: d["nodeTypes"]["llm"]["io"]["output"]["map"].update(text="text"),
            "nodeTypes.llm.io.output.map.text: path text must start at $",
        ),
        (
            lambda d: d["nodeTypes"]["llm"]["io"]["output"]["aliases"].update(answer="nothing"),
            "nodeTypes.llm.io.output.aliases.answer: nothing is not a field of the payload",
        ),
        (
            lambda d: d["nodeTypes"]["view"]["io"]["output"].update(extensions=["ext.none@1"]),
            "nodeTypes.view.io.output.extensions: unknown schema ext.none@1",
        ),
        (
            lambda d: d["nodeTypes"]["variable"].pop("io"),
            "nodeTypes.variable.io: missing (every compiled node type declares its input and output)",
        ),
    ],
)
def test_I01_a_bad_io_block_stops_start_up_naming_it(change: Any, expected: str) -> None:
    resources = resources_with(manifest_with(change))
    with pytest.raises(StartupError) as caught:
        build_schema_registry(resources)
    assert caught.value.code == "RESOURCE_INVALID"
    assert expected in caught.value.details["problems"]


# --- I-03, I-04: ports ----------------------------------------------------------------------


def bind(node_type: str, config: dict[str, Any], scope: TemplateScope | None = None) -> Any:
    entry = DEV.manifest.node_types[node_type]
    context = TemplateContext(
        scope=scope
        or TemplateScope(system={"userQuery": "from system"}, history=[{"role": "user", "content": "h"}])
    )
    return PortBinder(REGISTRY).bind(
        "n1", entry.io, config, 1, lambda v: RESOLVER.resolve(v, context), lambda p: RESOLVER.read(p, context)
    )


def test_I03_configuration_then_template_then_state_path() -> None:
    messages = [{"role": "system", "content": "be brief"}]
    from_config = bind(
        "llm",
        {"messages": messages, "userQueryMapping": "{{flow.q}}"},
        TemplateScope(flow={"q": "from config"}, system={"userQuery": "from system"}),
    )
    assert from_config.get("prompt") == "from config" and from_config.get("messages") == messages
    from_template = bind("llm", {"messages": messages, "userQueryMapping": "  "})
    assert from_template.get("prompt") == "from system"
    assert from_template.get("history") == [{"role": "user", "content": "h"}]


def test_I03_a_missing_required_port_fails_before_execute() -> None:
    with pytest.raises(ZonicError) as caught:
        bind("llm", {})
    assert caught.value.code == "INPUT_PORT_MISSING" and caught.value.details == {
        "node_id": "n1",
        "port": "messages",
    }


def test_I03_optional_ports_without_a_value_are_absent() -> None:
    bound = bind("approval", {})
    assert not bound.has("message") and dict(bound.all()) == {}


def test_I04_a_port_value_failing_its_schema_is_input_invalid() -> None:
    with pytest.raises(ZonicError) as caught:
        bind("llm", {"messages": [{"content": "no role"}]})
    assert caught.value.code == "INPUT_INVALID" and caught.value.details["port"] == "messages"
    assert caught.value.details["issues"][0]["path"] == "inputs.messages[0].role"


# --- I-05, I-06: output -----------------------------------------------------------------------


def test_I05_output_data_from_the_map_with_payload_defaults() -> None:
    mapper = OutputMapper(REGISTRY, "strict")
    io = DEV.manifest.node_types["agent"].io
    data = mapper.data(io, {"text": "hi", "toolResults": {"t": 1}, "fields": {"extra": 2, "text": "ignored"}})
    assert data["text"] == "hi" and data["toolResults"] == {"t": 1} and data["extra"] == 2
    assert data["toolCalls"] == []
    assert mapper.data(DEV.manifest.node_types["start"].io, {"a": 1}) == {"a": 1}


def test_I05_strict_fails_and_warn_keeps_the_output() -> None:
    schema = REGISTRY.schema("zonic.text@1")
    with pytest.raises(ZonicError) as caught:
        OutputMapper(REGISTRY, "strict").build("n1", schema, {"text": 5})
    assert caught.value.code == "OUTPUT_SCHEMA_INVALID"
    assert caught.value.details["issues"][0]["path"] == "output.text"
    output, issues = OutputMapper(REGISTRY, "warn").build("n1", schema, {"text": 5})
    assert output.data == {"text": 5} and [i.path for i in issues] == ["output.text"]


def test_I06_a_node_output_schema_refines_that_node_only() -> None:
    own = {"type": "object", "required": ["score"], "properties": {"score": {"type": "number"}}}
    blueprint = build(
        doc({"id": "a", "type": "variable", "config": {"outputSchema": own}}, {"id": "b", "type": "variable"})
    )
    mapper = OutputMapper(REGISTRY, "warn")
    _, refined = mapper.build("a", blueprint.nodes["a"].output_schema, {"other": 1})
    _, plain = mapper.build("b", blueprint.nodes["b"].output_schema, {"other": 1})
    assert [i.path for i in refined] == ["output.score"] and plain == []


# --- I-07, I-08: extending -------------------------------------------------------------------


def test_I07_a_new_output_field_by_extension_and_one_map_line() -> None:
    def change(d: dict[str, Any]) -> None:
        d["schemaExtensions"]["ext.db.score@1"] = {
            "type": "object",
            "properties": {"score": {"type": "number"}},
        }
        d["nodeTypes"]["db_query"]["io"]["output"]["extensions"] = ["ext.db.score@1"]
        d["nodeTypes"]["db_query"]["io"]["output"]["map"]["score"] = "$.score"

    resources = resources_with(manifest_with(change))
    registry = build_schema_registry(resources)
    io = resources.manifest.node_types["db_query"].io
    data = OutputMapper(registry, "strict").data(io, {"rows": [], "score": 0.9})
    assert data["score"] == 0.9
    db = {"id": "db", "type": "db_query", "config": {"query": "q"}}
    score = reader("{{nodes.db.output.score}}")
    assert "TEMPLATE_PATH_UNKNOWN" not in [w.code for w in build(doc(db, score), resources).warnings]
    assert "TEMPLATE_PATH_UNKNOWN" in [w.code for w in build(doc(db, score)).warnings]


@payload("zonic.sample", 1)
class SampleV1(ZonicModel):
    count: int = 0


@payload("zonic.sample", 2)
class SampleV2(ZonicModel):
    count: int = 0
    label: str


def test_I08_two_payload_versions_validate_separately() -> None:
    assert {"zonic.sample@1", "zonic.sample@2"} <= set(payload_models())
    registry = SchemaRegistry(DEV.manifest, DEV.builder, 16)
    assert registry.validate(registry.schema("zonic.sample@1"), {"count": 1}) == []
    assert [i.path for i in registry.validate(registry.schema("zonic.sample@2"), {"count": 1})] == [".label"]
    with pytest.raises(TypeError):
        payload("zonic.sample", 1)(SampleV2)


# --- I-09: static template check -----------------------------------------------------------


LLM = {
    "id": "llm_1",
    "type": "llm",
    "name": "Draft",
    "config": {"messages": [{"role": "user", "content": "x"}]},
}
#: A closed output (``zonic.rows@1`` has no spread), so unknown paths are visible.
DB = {"id": "db", "type": "db_query", "name": "Lookup", "config": {"query": "q"}}


def reader(template: str) -> dict[str, Any]:
    return {
        "id": "v",
        "type": "variable",
        "config": {"variableUpdates": [{"path": "flow.x", "value": template}]},
    }


@pytest.mark.parametrize(
    ("template", "unknown"),
    [
        ("{{nodes.db.output.unknown}}", True),
        ("{{nodes.Lookup.output.rowCount.deeper}}", True),
        ("{{nodes.ghost.output.text}}", True),
        ("{{nodes.db.output.rows.0.anyColumn}}", False),
        ("{{nodes.Lookup.rowCount}}", False),
        ("{{nodes.llm_1.output.text}}", False),
        ("{{nodes.llm_1.output.result}}", False),
        ("{{nodes.Draft.result}}", False),
        ("{{nodes.llm_1.status}}", False),
        ("{{nodes.llm_1.output.finishReason}}", False),
        ("{{flow.anything}}", False),
    ],
)
def test_I09_node_output_paths_are_checked_before_the_run(template: str, unknown: bool) -> None:
    findings = [
        w for w in build(doc(LLM, DB, reader(template))).warnings if w.code == "TEMPLATE_PATH_UNKNOWN"
    ]
    assert bool(findings) is unknown
    assert all(w.severity == "warning" and w.node_id == "v" for w in findings)


def test_I09_unknown_paths_can_be_plan_errors() -> None:
    with pytest.raises(ZonicError) as caught:
        build(doc(DB, reader("{{nodes.db.output.unknown}}")), resources_with(template_path_unknown="error"))
    assert caught.value.code == "TEMPLATE_PATH_UNKNOWN"


def test_I09_tool_results_names_and_output_structure_are_known() -> None:
    agent = {
        "id": "ag",
        "type": "agent",
        "config": {
            "tools": [{"toolType": "mcp", "name": "GetGlobalCode"}],
            "outputStructure": [{"target": "summary", "value": "{{nodeOutput.text}}"}],
        },
    }
    ok = build(doc(agent, reader("{{nodes.ag.toolResults.getglobalcode.x}} {{nodes.ag.output.summary}}")))
    assert "TEMPLATE_PATH_UNKNOWN" not in [w.code for w in ok.warnings]
    bad = build(doc(agent, reader("{{nodes.ag.toolResults.other_tool}}")))
    assert "TEMPLATE_PATH_UNKNOWN" in [w.code for w in bad.warnings]


def test_I09_specs_carry_the_effective_output_schema() -> None:
    blueprint = build(doc(LLM))
    assert blueprint.nodes["llm_1"].output_schema == REGISTRY.schema("zonic.text@1")
    assert blueprint.nodes["llm_1"].io == DEV.manifest.node_types["llm"].io


# --- I-10, I-11 ---------------------------------------------------------------------------------


def test_I10_node_type_description_matches_the_manifest() -> None:
    described = {d["type"]: d for d in describe_node_types(DEV.manifest, REGISTRY)}
    for node_type, entry in DEV.manifest.node_types.items():
        if entry.io is None:
            assert node_type not in described
            continue
        found = described[node_type]
        assert found["kind"] == entry.kind
        assert [p["name"] for p in found["inputs"]] == [p.name for p in entry.io.inputs]
        assert found["outputAliases"] == (entry.io.output.aliases if entry.io.output else {})
    assert described["view"]["outputSchema"]["allOf"][1] == REGISTRY.schema("ext.view.html@1")


def test_I11_a_new_node_type_mapped_to_an_existing_kind_needs_no_code() -> None:
    def change(d: dict[str, Any]) -> None:
        d["nodeTypes"]["summary_llm"] = copy.deepcopy(d["nodeTypes"]["llm"])

    resources = resources_with(manifest_with(change))
    build_schema_registry(resources)
    blueprint = build(
        doc({"id": "s1", "type": "summary_llm", "config": {"messages": [{"role": "user", "content": "x"}]}}),
        resources,
    )
    spec = blueprint.nodes["s1"]
    assert spec.kind == "llm" and spec.compiled and spec.handles == ["default"] and spec.output_schema


# --- B-10: output mappings ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "mapping",
    [
        [{"key": "answer", "value": "{{flow.a}}"}],
        [{"target": "answer", "source": "{{flow.a}}"}],
        {"answer": {"value": "{{flow.a}}"}},
    ],
    ids=["key-value", "target-source", "object"],
)
def test_B10_three_output_mapping_shapes_become_one_list(mapping: Any) -> None:
    out = {"id": "out", "type": "output", "config": {"outputMapping": mapping}}
    node = WorkflowLoader.from_resources(DEV).from_stored(doc(out)).document.node("out")
    assert node is not None
    entries = [OutputMappingEntry.model_validate(e) for e in node.config["outputMapping"]]
    assert [(e.target, e.value) for e in entries] == [("answer", "{{flow.a}}")]
    context = TemplateContext(scope=TemplateScope(flow={"a": 42}))
    assert build_mapping(entries, RESOLVER, context) == {"answer": 42}


@pytest.mark.parametrize(("node_type", "key"), [("output", "outputKey"), ("view", "outputVariable")])
def test_B10_output_key_and_output_variable_name_the_write_path(node_type: str, key: str) -> None:
    node = {"id": "n", "type": node_type, "config": {key: "flow.report"}}
    loaded = WorkflowLoader.from_resources(DEV).from_stored(doc(node)).document.node("n")
    assert loaded is not None and loaded.config.get("writeTo") == "flow.report" and key not in loaded.config
