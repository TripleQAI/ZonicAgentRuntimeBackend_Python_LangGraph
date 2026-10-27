"""Template resolver and conditions (P1-06: E-11, N-09)."""

from __future__ import annotations

from typing import Any

import pytest

from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import load_resources
from zonic_runtime.errors import ZonicError
from zonic_runtime.models.nodes import Condition, RuleBranch
from zonic_runtime.workflow.templates.conditions import ConditionEvaluator, select_branch
from zonic_runtime.workflow.templates.resolver import (
    TemplateContext,
    TemplateIssues,
    TemplateNode,
    TemplateResolver,
    TemplateScope,
    has_template,
)

DEV = load_resources(EnvironmentModel(env="dev"))
RESOLVER = TemplateResolver(DEV.templates)
EVALUATOR = ConditionEvaluator(DEV.dialects.canonical_operators)
NODES = {
    "llm_1": TemplateNode("llm_1", {"result": "text"}),
    "script_1": TemplateNode("script_1"),
    "agent_0": TemplateNode("agent_0", {"result": "text"}),
}
NAMES = {"Draft": "llm_1"}
SCOPE = TemplateScope(
    system={"userQuery": "hello", "attachments": []},
    flow={"count": 3, "flag": True, "items": [1, 2], "profile": {"name": "Ada"}},
    nodes={
        "llm_1": {"status": "succeeded", "visit": 2, "output": {"text": "drafted"}},
        "script_1": {"status": "succeeded", "visit": 1, "output": {"needsReview": True, "severity": 4}},
        "agent_0": {
            "status": "succeeded",
            "visit": 1,
            "output": {"text": "ok", "toolResults": {"GetGlobalCode": {"questionOptions": ["a"]}}},
        },
    },
    messages=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "x", "thread": "notes"}],
    run={"runId": "r1"},
    run_metadata={"workflowId": "wf-1"},
    interrupt_answer={"approved": True, "comment": "fine"},
)


def ctx(**kwargs: Any) -> TemplateContext:
    return TemplateContext(scope=SCOPE, nodes=lambda n: NODES.get(NAMES.get(n, n)), **kwargs)


def resolve(value: Any, issues: TemplateIssues | None = None, **kwargs: Any) -> Any:
    return RESOLVER.resolve(value, ctx(**kwargs), issues)


# --- E-11 ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("{{flow.count}}", 3),
        ("{{ flow.flag }}", True),
        ("{{flow.items}}", [1, 2]),
        ("{{flow.profile}}", {"name": "Ada"}),
        ("{{flow.items.1}}", 2),
        ("{{system.userQuery}}", "hello"),
        ("{{nodes.llm_1.output.text}}", "drafted"),
        ("{{nodes.llm_1.status}}", "succeeded"),
        ("{{nodes.llm_1.visit}}", 2),
        ("{{messages.0.content}}", "hi"),
        ("{{thread.notes.0.content}}", "x"),
        ("{{run.runId}}", "r1"),
    ],
)
def test_E11_an_exact_token_gives_the_typed_value(template: str, expected: Any) -> None:
    assert resolve(template) == expected


def test_E11_embedded_tokens_become_text_with_objects_as_json() -> None:
    assert resolve("n={{flow.count}} p={{flow.profile}} l={{flow.items}}") == 'n=3 p={"name":"Ada"} l=[1,2]'
    assert resolve({"a": ["{{flow.count}}", "x {{flow.flag}}"]}) == {"a": [3, "x true"]}


def test_E11_a_missing_path_is_null_exactly_and_empty_embedded_and_is_reported() -> None:
    issues = TemplateIssues()
    assert resolve("{{flow.nope}}", issues) is None
    assert resolve("a{{nodes.ghost.output}}b", issues) == "ab"
    assert issues.missing == ["flow.nope", "nodes.ghost.output"]


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("{{node.llm_1.output.text}}", "{{nodes.llm_1.output.text}}"),
        ("{{variable.count}}", "{{flow.count}}"),
        ("{{nodes.Draft.output.text}}", "{{nodes.llm_1.output.text}}"),
        ("{{nodes.llm_1.output.result}}", "{{nodes.llm_1.output.text}}"),
        ("{{nodes.llm_1.result}}", "{{nodes.llm_1.output.text}}"),
    ],
)
def test_E11_aliases_give_the_same_values(alias: str, canonical: str) -> None:
    assert resolve(alias) == resolve(canonical) is not None


def test_E11_special_roots() -> None:
    assert resolve("{{approval.response.comment}}") == "fine"
    assert resolve("{{human.response}}") == {"approved": True, "comment": "fine"}
    assert resolve("{{runtime.workflowMetaData.workflowId}}") == "wf-1"
    assert resolve("{{nodeOutput.text}}", node_id="llm_1") == "drafted"
    assert resolve("{{nodeOutput.result}}", node_id="llm_1") == "drafted"
    building = {"score": 9}
    assert resolve("{{nodeOutput.score}}", node_id="llm_1", node_output=building) == 9
    assert resolve("{{nodes.llm_1.output.score}}", node_id="llm_1", node_output=building) == 9


def test_E11_node_path_alias_reads_the_whole_output_and_tool_results_ignore_case() -> None:
    assert resolve("{{nodes.script_1.result.needsReview}}") is True
    assert resolve("{{nodes.script_1.output.result.severity}}") == 4
    assert resolve("{{nodes.agent_0.toolResults.getglobalcode.questionOptions}}") == ["a"]


def test_E11_bare_names_read_flow_and_are_reported() -> None:
    issues = TemplateIssues()
    assert resolve("{{count}}", issues) == 3 and issues.bare == ["count"]
    strict = TemplateResolver(DEV.templates.model_copy(update={"bare_names": "error"}))
    assert strict.resolve("{{count}}", ctx()) is None


def test_E11_no_evaluation_and_template_detection() -> None:
    assert resolve("{{flow.count + 1}}") is None
    assert resolve("{{__import__('os')}}") is None
    assert has_template("a {{x}}") and not has_template("a {x}") and not has_template(3)


# --- N-09 -------------------------------------------------------------------------------------


def cond(field: Any, op: str, value: Any = None) -> Condition:
    return Condition(field=field, op=op, value=value)


@pytest.mark.parametrize(
    ("left", "op", "right", "expected"),
    [
        (3, "eq", "3", True),
        (True, "eq", "true", True),
        ("  yes ", "eq", "yes", True),
        (3, "ne", 4, True),
        (False, "ne", "false", False),
        (10, "gt", "9", True),
        ("10", "gt", "9", True),
        ("b", "gt", "a", True),
        (5, "gte", 5, True),
        (4, "lt", 5, True),
        (5, "lte", 4, False),
        ("abc", "gt", 1, False),
        ("hello world", "contains", "world", True),
        ([1, 2, 3], "contains", "2", True),
        ({"k": 1}, "contains", "k", True),
        (None, "contains", "x", False),
        ([1], "not_contains", 2, True),
        (0, "exists", None, True),
        (None, "exists", None, False),
        (None, "not_exists", None, True),
        ("  ", "empty", None, True),
        ([], "empty", None, True),
        ({}, "empty", None, True),
        (0, "empty", None, False),
        ("x", "not_empty", None, True),
    ],
)
def test_N09_every_operator_with_types(left: Any, op: str, right: Any, expected: bool) -> None:
    assert EVALUATOR.test(cond(left, op, right), lambda v: v) is expected


def test_N09_and_or_and_empty_lists() -> None:
    yes, no = cond(1, "eq", 1), cond(1, "eq", 2)
    ident = lambda v: v  # noqa: E731
    assert EVALUATOR.all([yes, yes], "AND", ident) and not EVALUATOR.all([yes, no], "AND", ident)
    assert EVALUATOR.all([no, yes], "OR", ident) and not EVALUATOR.all([no, no], "or", ident)
    assert EVALUATOR.all([], "AND", ident) and not EVALUATOR.all([], "OR", ident)


def test_N09_fields_and_values_are_templates() -> None:
    def res(v: Any) -> Any:
        return resolve(v)

    assert EVALUATOR.test(cond("{{flow.count}}", "gte", "{{flow.items.1}}"), res)


def test_N09_unknown_operator_is_a_plan_error() -> None:
    with pytest.raises(ZonicError, match="PLAN_INVALID"):
        EVALUATOR.test(cond(1, "roughly", 1), lambda v: v)
    with pytest.raises(ValueError):
        ConditionEvaluator(["eq", "telepathy"])


def branch(rule_id: str, *conditions: Condition, **kwargs: Any) -> RuleBranch:
    return RuleBranch(rule_id=rule_id, conditions=list(conditions), **kwargs)


def test_N09_first_matching_enabled_branch_wins() -> None:
    branches = [
        branch("r_off", cond(1, "eq", 1), enabled=False),
        branch("r_no", cond(1, "eq", 2)),
        branch("r_yes", cond(1, "eq", 1)),
        branch("r_also", cond(1, "eq", 1)),
        branch("r_else", is_default=True),
    ]
    choice = select_branch(branches, DEV.dialects.default_rule_ids, EVALUATOR, lambda v: v)
    assert (choice.handle, choice.matched) == ("r_yes", "r_yes")


def test_N09_no_match_takes_the_default_branch() -> None:
    branches = [branch("r_no", cond(1, "eq", 2)), branch("r_empty"), branch("r_fallback", is_default=True)]
    choice = select_branch(branches, DEV.dialects.default_rule_ids, EVALUATOR, lambda v: v)
    assert (choice.handle, choice.matched) == ("r_fallback", None)


def test_N09_without_is_default_the_default_rule_id_with_an_edge_is_used() -> None:
    branches = [branch("r_no", cond(1, "eq", 2))]
    ids = DEV.dialects.default_rule_ids
    assert select_branch(branches, ids, EVALUATOR, lambda v: v, outgoing={"r-else"}).handle == "r-else"
    assert select_branch(branches, ids, EVALUATOR, lambda v: v, outgoing={"r_else"}).handle == "r_else"
    assert select_branch(branches, ids, EVALUATOR, lambda v: v, outgoing=set()).handle is None
