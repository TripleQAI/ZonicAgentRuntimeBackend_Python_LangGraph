"""Dialect normalization over every vendored document (P1-03, ``tests/compat/``).

Cases B-01 to B-11, B-14, B-15, D-12, D-13, F-16 and F-02 (normalization parts). The
runtime halves of these cases are proven by the tasks that build that behavior (plan:
B-01 also P1-05 and P1-09, B-05 P3-06, B-06 and B-07 P1-08, B-08 and B-09 P1-06, B-10
P1-07, B-02 to B-04 P2-02, D-13 P2-02).
"""

from __future__ import annotations

import copy
import json
import re
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from tests.workflow_fixtures import BUILDER, SYNTHETIC, document_files, fixture_id, read_json
from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import load_resources
from zonic_runtime.models.dialects import ModelRef, OutputMappingEntry, RoutingToolRef, ToolRef
from zonic_runtime.models.document import WorkflowDocument
from zonic_runtime.models.engine import StateUpdate
from zonic_runtime.models.nodes import NodeConfigBase, config_model_for
from zonic_runtime.workflow.loader.normalizer import DocumentNormalizer, NormalizedDocument

RESOURCES = load_resources(EnvironmentModel(env="dev"))
KIND = {name: entry.kind for name, entry in RESOURCES.manifest.node_types.items()}
OPERATORS = set(RESOURCES.dialects.canonical_operators)
FILES = document_files()
PLATFORM = sorted((BUILDER / "platform").glob("*.json"))
BUILDER_DIALECT = [p for p in FILES if p.parent.name != "platform"]
UPDATE_LISTS = ("variableUpdates", "stateUpdates", "updates")
TEMPLATE = re.compile(r"\{\{[^{}]+\}\}")


def normalizer(rule_expression: str = "unsupported") -> DocumentNormalizer:
    return DocumentNormalizer(
        RESOURCES.dialects,
        KIND.get,
        default_model=RESOURCES.models.default_model,
        rule_expression=rule_expression,  # type: ignore[arg-type]
    )


def stored(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = read_json(path)
    return data["document"] if path.parent.name == "templates" else data


@cache
def normalized(path: Path) -> NormalizedDocument:
    return normalizer().normalize(stored(path))


def configs(document: WorkflowDocument) -> list[tuple[str, str, NodeConfigBase]]:
    return [(n.id, n.type, config_model_for(KIND[n.type]).model_validate(n.config)) for n in document.nodes]


def compat_problems(result: NormalizedDocument) -> list[str]:
    """Every construct no dialect rule or model covers, named (case B-15)."""
    problems = [
        f"{f.details.get('path')}: unknown field"
        for f in result.findings
        if f.code == "DOCUMENT_UNKNOWN_FIELD"
    ]
    for node in result.document.nodes:
        config = config_model_for(KIND[node.type]).model_validate(node.config)
        problems += [f"{node.id}.config.{k}: unknown configuration key" for k in config.unknown_keys()]
        for key in UPDATE_LISTS:
            for i, update in enumerate(node.config.get(key) or []):
                try:
                    parsed = StateUpdate.model_validate(update)
                except ValidationError as exc:
                    problems.append(f"{node.id}.config.{key}[{i}]: {exc.errors()[0]['msg']}")
                    continue
                for c in parsed.when.conditions if parsed.when else []:
                    if c.op not in OPERATORS:
                        problems.append(f"{node.id}.config.{key}[{i}]: operator {c.op!r} has no alias")
        for rule in node.config.get("rules") or []:
            for c in rule.get("conditions") or []:
                if c.get("op") not in OPERATORS:
                    problems.append(
                        f"{node.id} rule {rule.get('ruleId')}: operator {c.get('op')!r} has no alias"
                    )
    return problems


# --- B-01, B-15: every document normalizes; nothing uncovered ---------------------------


@pytest.mark.parametrize("path", FILES, ids=[fixture_id(p) for p in FILES])
def test_B01_every_document_normalizes_and_validates(path: Path) -> None:
    result = normalized(path)
    assert configs(result.document)
    assert result.document.unknown_key_report() == []


@pytest.mark.parametrize("path", FILES, ids=[fixture_id(p) for p in FILES])
def test_B15_no_construct_is_left_uncovered(path: Path) -> None:
    assert compat_problems(normalized(path)) == []


def test_B15_an_uncovered_construct_is_named() -> None:
    document = copy.deepcopy(stored(BUILDER / "platform" / "quick-pay-assistant.json"))
    rule = next(n for n in document["nodes"] if n["type"] in {t for t, k in KIND.items() if k == "router"})
    rule["config"]["rules"][0]["conditions"][0]["operator"] = "is roughly"
    document["nodes"][0]["config"]["brandNewKey"] = 1
    document["mysteryField"] = True
    problems = compat_problems(normalizer().normalize(document))
    assert any("is roughly" in p for p in problems)
    assert any("brandNewKey" in p for p in problems)
    assert any("mysteryField" in p for p in problems)


def test_B01_the_stored_document_is_never_changed() -> None:
    path = BUILDER / "platform" / "intelligent-buying-orchestrator.json"
    before = stored(path)
    snapshot = copy.deepcopy(before)
    normalizer().normalize(before)
    assert before == snapshot


# --- B-02, F-02: models -------------------------------------------------------------


def _model_nodes(document: WorkflowDocument) -> list[Any]:
    return [n for n in document.nodes if "model" in n.config]


@pytest.mark.parametrize("path", FILES, ids=[fixture_id(p) for p in FILES])
def test_B02_every_model_becomes_a_model_ref(path: Path) -> None:
    for node in _model_nodes(normalized(path).document):
        ref = ModelRef.model_validate(node.config["model"])
        assert ref.model_id, node.id


def test_B02_model_objects_keep_provider_and_parameters() -> None:
    fields = {
        "code": "model_id",
        "provider": "provider",
        "vendor": "vendor",
        "name": "name",
        "temperature": "temperature",
        "topP": "top_p",
        "maxTokens": "max_tokens",
    }
    checked = 0
    for path in PLATFORM:
        document = normalized(path).document
        for raw in stored(path)["nodes"]:
            model = raw.get("config", {}).get("model")
            if not isinstance(model, dict):
                continue
            node = document.node(raw["id"])
            assert node is not None
            ref = ModelRef.model_validate(node.config["model"])
            for old, new in fields.items():
                if model.get(old) is not None:
                    assert getattr(ref, new) == model[old], (path.stem, raw["id"], old)
            checked += 1
    refs = [
        ModelRef.model_validate(n.config["model"])
        for p in PLATFORM
        for n in _model_nodes(normalized(p).document)
    ]
    assert checked and any(r.provider == "azureopenai" and r.model_id == "gpt-5.2" for r in refs)
    assert any(r.temperature is not None for r in refs) and any(r.top_p is not None for r in refs)


@pytest.mark.parametrize("path", sorted((BUILDER / "templates").glob("*.json")), ids=lambda p: p.stem)
def test_F02_empty_model_resolves_to_the_catalog_default_with_a_warning(path: Path) -> None:
    result = normalized(path)
    defaulted = [f for f in result.findings if f.code == "MODEL_DEFAULTED"]
    assert defaulted and all(f.severity == "warning" for f in defaulted)
    for finding in defaulted:
        node = result.document.node(finding.node_id or "")
        assert node is not None
        assert ModelRef.model_validate(node.config["model"]).model_id == RESOURCES.models.default_model


# --- B-03: promptTemplate ----------------------------------------------------------


def test_B03_prompt_template_becomes_system_prompt_and_messages() -> None:
    raw_agents = [
        (p, n) for p in PLATFORM for n in stored(p)["nodes"] if n.get("config", {}).get("promptTemplate")
    ]
    assert raw_agents
    for path, raw in raw_agents:
        node = normalized(path).document.node(raw["id"])
        assert node is not None and "promptTemplate" not in node.config
        entries = raw["config"]["promptTemplate"]
        system = [e["text"] for e in entries if e.get("role") == "system"]
        if KIND[raw["type"]] == "agent" and system:
            assert node.config["systemPrompt"] == "\n\n".join(system)
        others = [e for e in entries if KIND[raw["type"]] != "agent" or e.get("role") != "system"]
        assert [m["content"] for m in node.config.get("messages", [])][-len(others) or None :] == [
            e["text"] for e in others
        ][-len(others) or None :]


# --- B-04: routing tools ------------------------------------------------------------


def test_B04_routing_tools_and_handoff_edges() -> None:
    routing, handoff_edges = 0, 0
    for path in PLATFORM:
        document = normalized(path).document
        for node in document.nodes:
            for raw in node.config.get("routingTools") or []:
                tool = RoutingToolRef.model_validate(raw)
                edges = [e for e in document.edges if e.source == node.id and e.target == tool.target]
                assert any(e.source_handle == f"handoff-{tool.target}" for e in edges), (
                    path.stem,
                    tool.call_name,
                )
                routing += 1
        handoff_edges += sum(1 for e in document.edges if (e.source_handle or "").startswith("handoff-"))
    assert routing == 59 and handoff_edges >= routing


def test_B04_ordinary_tools_become_tool_refs() -> None:
    count = 0
    for path in FILES:
        for node in normalized(path).document.nodes:
            for tool in node.config.get("tools") or []:
                if isinstance(tool, dict) and "toolType" in tool:
                    ToolRef.model_validate(tool)
                    count += 1
    assert count >= 22


# --- B-05: tool nodes ---------------------------------------------------------------


def test_B05_tool_nodes_carry_tool_type_and_server() -> None:
    tool_nodes = [n for p in PLATFORM for n in normalized(p).document.nodes if KIND[n.type] == "tool"]
    assert len(tool_nodes) == 53
    for node in tool_nodes:
        assert node.config.get("toolType") and node.config.get("toolId")
        assert "type" not in node.config


# --- B-06, B-07: updates ------------------------------------------------------------


def test_B06_node_level_updates_become_canonical_state_updates() -> None:
    moved = 0
    for path in PLATFORM:
        raw_nodes = {n["id"]: n for n in stored(path)["nodes"]}
        for node in normalized(path).document.nodes:
            node_level = raw_nodes[node.id].get("variableUpdates") or []
            assert not node.variable_updates
            if node_level:
                updates = [StateUpdate.model_validate(u) for u in node.config["variableUpdates"]]
                assert {u.path for u in updates} >= {u["fieldName"] for u in node_level}
                moved += len(node_level)
    assert moved == 277


def test_B06_update_rules_become_a_when_condition() -> None:
    whens = [
        StateUpdate.model_validate(u).when
        for p in PLATFORM
        for n in normalized(p).document.nodes
        for u in n.config.get("variableUpdates") or []
        if "when" in u
    ]
    assert len(whens) == 44 and all(w and w.conditions for w in whens)


def test_B07_thread_and_system_targets_are_kept_as_paths() -> None:
    paths = {
        StateUpdate.model_validate(u).path
        for p in PLATFORM
        for n in normalized(p).document.nodes
        for u in n.config.get("variableUpdates") or []
    }
    assert any(p.startswith("thread.") for p in paths)
    assert {"system.userQuery", "system.attachments"} & paths


# --- B-08: rules ----------------------------------------------------------------------


def test_B08_platform_rules_read_like_builder_rules() -> None:
    branches = [
        b for p in PLATFORM for n in normalized(p).document.nodes for b in n.config.get("rules") or []
    ]
    assert branches
    for branch in branches:
        assert "enable" not in branch and "logicType" not in branch
        assert branch["conditionLogic"] in {"AND", "OR"} and isinstance(branch["ruleId"], str)
        assert all(c["op"] in OPERATORS and "operator" not in c for c in branch.get("conditions") or [])
    defaults = [
        b for b in branches if b.get("isDefault") or b.get("ruleId") in RESOURCES.dialects.default_rule_ids
    ]
    assert defaults


# --- B-09: templates are left for the resolver ----------------------------------------


def _templates(value: Any) -> set[str]:
    return set(TEMPLATE.findall(json.dumps(value)))


@pytest.mark.parametrize("path", FILES, ids=[fixture_id(p) for p in FILES])
def test_B09_every_template_survives_normalization(path: Path) -> None:
    after = _templates(normalized(path).document.model_dump(by_alias=True, mode="json"))
    assert _templates(stored(path)) - after == set()


def test_B04_routing_tools_keep_preset_parameters() -> None:
    document = normalized(BUILDER / "platform" / "intelligent-buying-orchestrator.json").document
    tools = [
        RoutingToolRef.model_validate(t) for n in document.nodes for t in n.config.get("routingTools") or []
    ]
    preset = next(t for t in tools if t.alias == "get_decision_points")
    assert preset.parameters and preset.parameters["data"]["category"] == "{{flow.qa_master_data.category}}"
    assert preset.enabled and not preset.return_direct


# --- B-10, B-11: output mappings and views --------------------------------------------


@pytest.mark.parametrize("path", FILES, ids=[fixture_id(p) for p in FILES])
def test_B10_every_output_mapping_is_one_canonical_list(path: Path) -> None:
    adapter = TypeAdapter(list[OutputMappingEntry])
    for node in normalized(path).document.nodes:
        if "outputMapping" in node.config:
            entries = adapter.validate_python(node.config["outputMapping"])
            assert all(e.target for e in entries)


def test_B10_output_key_and_output_variable_become_write_to() -> None:
    written = [n for p in FILES for n in normalized(p).document.nodes if "writeTo" in n.config]
    assert written
    assert not [
        n
        for p in FILES
        for n in normalized(p).document.nodes
        if {"outputKey", "outputVariable"} & set(n.config)
    ]


def test_B11_view_configurations_are_kept() -> None:
    views = [n for p in FILES for n in normalized(p).document.nodes if KIND[n.type] == "view"]
    assert views and any(n.config.get("htmlTemplate") for n in views)
    assert any(n.config.get("viewMode") == "template_config" for n in views)


# --- B-14, F-16: which rules fire -----------------------------------------------------


@pytest.mark.parametrize("path", BUILDER_DIALECT, ids=[fixture_id(p) for p in BUILDER_DIALECT])
def test_B14_builder_documents_normalize_to_themselves_except_aliases(path: Path) -> None:
    result = normalized(path)
    n = normalizer()
    assert {n.rule_class(r) for r in result.fired_rules} <= {"alias"}
    raw = stored(path)
    assert [x.id for x in result.document.nodes] == [x["id"] for x in raw["nodes"]]
    assert [(e.source, e.target, e.source_handle) for e in result.document.edges] == [
        (e["source"], e["target"], e.get("sourceHandle")) for e in raw["edges"]
    ]


@pytest.mark.parametrize("path", PLATFORM, ids=lambda p: p.stem)
def test_F16_platform_documents_use_documented_rules_each_listed(path: Path) -> None:
    result = normalized(path)
    fired = {f.details["rule_id"] for f in result.findings if f.code == "DIALECT_RULE"}
    assert fired and fired <= {r.id for r in RESOURCES.dialects.rules}
    assert any(normalizer().rule_class(r) == "platform" for r in fired)
    assert all(f.message for f in result.findings)


# --- D-12, D-13 --------------------------------------------------------------------------


def test_D12_rule_expression_is_unsupported_by_default() -> None:
    result = normalized(SYNTHETIC / "rule-expression.json")
    errors = [f for f in result.errors if f.code == "RULE_EXPRESSION_UNSUPPORTED"]
    assert len(errors) == 2


def test_D12_parse_simple_converts_a_simple_expression() -> None:
    result = normalizer("parse_simple").normalize(stored(SYNTHETIC / "rule-expression.json"))
    assert not result.errors
    rule = next(n for n in result.document.nodes if KIND[n.type] == "router")
    by_id = {b["ruleId"]: b for b in rule.config["rules"]}
    assert by_id["r_high"]["conditions"] == [{"field": "{{flow.score}}", "op": "gte", "value": 8}]
    assert by_id["r_named"]["conditions"] == [{"field": "{{flow.name}}", "op": "eq", "value": "alpha"}]
    assert "expression" not in by_id["r_high"]


def test_D12_parse_simple_still_rejects_a_complex_expression() -> None:
    document = copy.deepcopy(stored(SYNTHETIC / "rule-expression.json"))
    rule = next(n for n in document["nodes"] if n["id"] == "rule_1")
    rule["config"]["rules"][0]["expression"] = "{{flow.score}} >= 8 and {{flow.name}} == 'alpha'"
    errors = [f for f in normalizer("parse_simple").normalize(document).errors]
    assert [f.code for f in errors] == ["RULE_EXPRESSION_UNSUPPORTED"]


def test_D13_output_without_mapping_warns() -> None:
    result = normalized(BUILDER / "zonic" / "test-agent-workflow.json")
    missing = [f for f in result.findings if f.code == "OUTPUT_MAPPING_MISSING"]
    assert missing and all(f.severity == "warning" for f in missing)
    assert not [
        f
        for f in normalized(BUILDER / "templates" / "approval_flow.json").findings
        if f.code == "OUTPUT_MAPPING_MISSING"
    ]


def test_D12_an_unsupported_agent_strategy_is_an_error() -> None:
    document = copy.deepcopy(stored(SYNTHETIC / "supervisor-loop.json"))
    document["nodes"][1]["config"]["strategy"] = "plan_exec"
    errors = normalizer().normalize(document).errors
    assert [f.code for f in errors] == ["DOCUMENT_INVALID"]
