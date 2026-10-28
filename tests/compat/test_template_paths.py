"""Templates and rules of the vendored documents (P1-06: F-05, B-08, B-09)."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tests.workflow_fixtures import BUILDER, read_json
from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import load_resources
from zonic_runtime.models.blueprint import Blueprint
from zonic_runtime.models.nodes import RuleBranch
from zonic_runtime.workflow.blueprint.builder import BlueprintBuilder
from zonic_runtime.workflow.loader.loader import WorkflowLoader
from zonic_runtime.workflow.templates.conditions import ConditionEvaluator, select_branch
from zonic_runtime.workflow.templates.resolver import (
    TemplateContext,
    TemplateIssues,
    TemplateResolver,
    TemplateScope,
    node_lookup,
)

DEV = load_resources(EnvironmentModel(env="dev"))
RESOURCES = replace(
    DEV,
    settings=DEV.settings.model_copy(
        update={"features": DEV.settings.features.model_copy(update={"script_node": True})}
    ),
)
LOADER = WorkflowLoader.from_resources(RESOURCES)
RESOLVER = TemplateResolver(RESOURCES.templates)
EVALUATOR = ConditionEvaluator(RESOURCES.dialects.canonical_operators)
NODE_TEMPLATE = re.compile(r"\{\{\s*(nodes?\.[^{}]+?)\s*\}\}")


def plan(path: Path) -> Blueprint:
    return BlueprintBuilder(RESOURCES).build(LOADER.from_stored(read_json(path), path.stem))


def node_templates(blueprint: Blueprint, document: Any) -> list[str]:
    return sorted(set(NODE_TEMPLATE.findall(json.dumps(document))))


# --- F-05: output paths -----------------------------------------------------------------------

#: Output data by node kind, shaped like the kinds' payloads (P1-07 defines them precisely).
OUTPUT_BY_KIND: dict[str, dict[str, Any]] = {
    "llm": {"text": "drafted"},
    "agent": {"text": "answered", "toolResults": {}},
    "external_agent": {"text": "remote"},
    "multi_agent": {"text": "swarm report", "finalAgent": "a", "nodeHistory": ["a"]},
    "subflow": {"result": "child result"},
    "sandbox": {"needsReview": True, "severity": "high"},
    "view": {"html": "<p/>"},
    "http": {"status": 200, "body": {}},
}
STRUCTURED = {
    "facilityName": "Plant 1",
    "includeRisk": True,
    "intent": "report",
    "periodEnd": "2026-01",
    "periodStart": "2025-12",
    "referencePeriodEnd": "x",
    "referencePeriodStart": "y",
    "utilityType": "power",
}


@pytest.mark.parametrize("name", ["approval_flow.json", "facility_operations_swarm.json"])
def test_F05_output_paths_resolve(name: str) -> None:
    path = BUILDER / "templates" / name
    blueprint = plan(path)
    records: dict[str, Any] = {}
    for spec in blueprint.nodes.values():
        output = copy.deepcopy(OUTPUT_BY_KIND.get(spec.kind, {"value": 1}))
        # An LLM with structured output (a schema, or responseFormat json) keeps the object at ``result``.
        if spec.config.output_schema or getattr(spec.config, "response_format", None) == "json":
            output["result"] = STRUCTURED
        records[spec.node_id] = {"status": "succeeded", "visit": 1, "output": output}
    context = TemplateContext(
        scope=TemplateScope(nodes=records), nodes=node_lookup(blueprint, RESOURCES.manifest)
    )
    templates = node_templates(blueprint, read_json(path))
    assert templates
    for template in templates:
        issues = TemplateIssues()
        assert RESOLVER.read(template, context, issues) is not None, template
        assert not issues.bare


def test_F05_text_alias_script_path_and_structured_result() -> None:
    blueprint = plan(BUILDER / "templates" / "facility_operations_swarm.json")
    script = next(s for s in blueprint.nodes.values() if s.kind == "sandbox")
    records = {script.node_id: {"status": "succeeded", "visit": 1, "output": {"needsReview": False}}}
    context = TemplateContext(
        scope=TemplateScope(nodes=records), nodes=node_lookup(blueprint, RESOURCES.manifest)
    )
    assert RESOLVER.read(f"nodes.{script.name}.result.needsReview", context) is False


# --- B-08: platform rules --------------------------------------------------------------------


def _branches(document: dict[str, Any]) -> list[RuleBranch]:
    node = LOADER.from_stored(document).document.nodes[1]
    return [RuleBranch.model_validate(b) for b in node.config["rules"]]


def _rule_document(rules: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "nodes": [{"id": "s", "type": "start"}, {"id": "r", "type": "rule", "config": {"rules": rules}}],
        "edges": [],
    }


PLATFORM_RULES = [
    {
        "ruleId": "r-big",
        "enable": True,
        "logicType": "AND",
        "conditions": [{"field": "{{flow.n}}", "operator": ">=", "value": 10}],
    },
    {
        "ruleId": "r-empty",
        "enable": True,
        "logicType": "AND",
        "conditions": [{"field": "{{flow.knowledge}}", "operator": "is empty"}],
    },
    {
        "ruleId": "r-off",
        "enable": False,
        "logicType": "AND",
        "conditions": [{"field": "1", "operator": "==", "value": 1}],
    },
    {"ruleId": "r-else", "enable": True, "logicType": "default", "conditions": []},
]
BUILDER_RULES = [
    {
        "ruleId": "r_big",
        "conditionLogic": "AND",
        "conditions": [{"field": "{{flow.n}}", "op": "gte", "value": 10}],
    },
    {
        "ruleId": "r_empty",
        "conditionLogic": "AND",
        "conditions": [{"field": "{{flow.knowledge}}", "op": "empty"}],
    },
    {
        "ruleId": "r_off",
        "enabled": False,
        "conditionLogic": "AND",
        "conditions": [{"field": "1", "op": "eq", "value": 1}],
    },
    {"ruleId": "r_else", "isDefault": True, "conditions": []},
]


@pytest.mark.parametrize(
    ("flow", "expected"),
    [
        ({"n": 12, "knowledge": "k"}, "big"),
        ({"n": 1, "knowledge": " "}, "empty"),
        ({"n": 1, "knowledge": "k"}, "else"),
    ],
)
def test_B08_platform_rules_take_the_same_branch_as_builder_rules(
    flow: dict[str, Any], expected: str
) -> None:
    context = TemplateContext(scope=TemplateScope(flow=flow))

    def resolve(value: Any) -> Any:
        return RESOLVER.resolve(value, context)

    chosen = []
    for rules in (PLATFORM_RULES, BUILDER_RULES):
        choice = select_branch(
            _branches(_rule_document(rules)), RESOURCES.dialects.default_rule_ids, EVALUATOR, resolve
        )
        assert choice.handle is not None
        chosen.append(choice.handle[2:])
    assert chosen == [expected, expected]


def test_B08_every_platform_rule_node_selects_a_branch() -> None:
    for path in sorted((BUILDER / "platform").glob("*.json")):
        document = LOADER.from_stored(read_json(path)).document
        for node in document.nodes:
            if "rules" not in node.config:
                continue
            branches = [RuleBranch.model_validate(b) for b in node.config["rules"]]
            choice = select_branch(branches, RESOURCES.dialects.default_rule_ids, EVALUATOR, lambda v: None)
            assert choice.handle in {b.rule_id for b in branches}, (path.stem, node.id)


# --- B-09: nodeOutput and toolResults --------------------------------------------------------


def _knowledge_agent(name: str, tool_results: dict[str, Any]) -> tuple[Blueprint, TemplateContext]:
    blueprint = plan(BUILDER / "platform" / f"{name}.json")
    agent = blueprint.resolve("knowledge_agent")
    assert agent is not None
    records = {
        agent.node_id: {
            "status": "succeeded",
            "visit": 1,
            "output": {"text": "answer", "toolResults": tool_results},
        }
    }
    context = TemplateContext(
        scope=TemplateScope(nodes=records),
        nodes=node_lookup(blueprint, RESOURCES.manifest),
        node_id=agent.node_id,
    )
    return blueprint, context


def test_B09_node_output_and_tool_results_resolve_by_tool_alias() -> None:
    # The knowledge tool quick_pay_complete_questionnaire.xlsx has settings.alias quick_pay_complete_questionnaire.
    blueprint, context = _knowledge_agent(
        "quick-pay-assistant",
        {
            "quick_pay_complete_questionnaire": {"text": "sheet"},
            "Save_Questionnaire_Response": {"flowStatus": "done"},
        },
    )
    assert RESOLVER.read("nodeOutput.text", context) == "answer"
    assert RESOLVER.read("nodeOutput.toolResults.quick_pay_complete_questionnaire.text", context) == "sheet"
    assert (
        RESOLVER.read("nodes.knowledge_agent.toolResults.save_questionnaire_response.flowStatus", context)
        == "done"
    )
    assert not [w for w in blueprint.warnings if w.code == "TEMPLATE_PATH_UNKNOWN"]


def test_B09_the_stale_reference_in_knowledge_agent_warns_and_is_null() -> None:
    # The tool's alias is "Fast Track Buying New Questionnaire"; the template names fast_track_buying_new_questionnaire.
    blueprint, context = _knowledge_agent(
        "fast-track-questionnaire-assistant", {"Fast Track Buying New Questionnaire": {"text": "x"}}
    )
    stale = "nodeOutput.toolResults.fast_track_buying_new_questionnaire.text"
    issues = TemplateIssues()
    assert RESOLVER.resolve("{{" + stale + "}}", context, issues) is None and issues.missing == [stale]
    unknown = [w for w in blueprint.warnings if w.code == "TEMPLATE_PATH_UNKNOWN"]
    assert [w.details["template"] for w in unknown] == [stale] and unknown[0].severity == "warning"


def test_B09_every_tool_results_reference_names_a_known_node() -> None:
    for path in sorted((BUILDER / "platform").glob("*.json")):
        blueprint = plan(path)
        lookup = node_lookup(blueprint, RESOURCES.manifest)
        for template in re.findall(r"\{\{\s*nodes\.([^.{}]+)\.toolResults", json.dumps(read_json(path))):
            assert lookup(template) is not None, (path.stem, template)
