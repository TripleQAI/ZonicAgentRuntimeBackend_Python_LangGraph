"""State operations and output fields (P1-06: E-12, F-04)."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import load_resources
from zonic_runtime.errors import ZonicError
from zonic_runtime.workflow.loader.loader import WorkflowLoader
from zonic_runtime.workflow.templates.conditions import ConditionEvaluator
from zonic_runtime.workflow.templates.resolver import TemplateContext, TemplateResolver, TemplateScope
from zonic_runtime.workflow.templates.updates import (
    REMOVED,
    UpdateResult,
    UpdateWriter,
    apply_op,
    build_mapping,
    convert_mapped,
)

DEV = load_resources(EnvironmentModel(env="dev"))
RESOLVER = TemplateResolver(DEV.templates)
WRITER = UpdateWriter(
    RESOLVER,
    ConditionEvaluator(DEV.dialects.canonical_operators),
    writable=DEV.templates.writable,
    writable_system_keys=DEV.settings.templates.writable_system_keys,
    state_ops=DEV.builder.state_ops,
)
FLOW = {"n": 1, "tags": ["a"], "profile": {"name": "Ada", "langs": ["en"]}, "gone": "x", "draft": "text"}


def run(*updates: dict[str, Any], flow: dict[str, Any] | None = None, **scope: Any) -> UpdateResult:
    source = TemplateScope(flow=flow if flow is not None else copy.deepcopy(FLOW), **scope)
    context = TemplateContext(scope=source, node_id="n1", node_output={"answer": 42, "items": [7, 8]})
    return WRITER.apply(list(updates), context)


def test_E12_the_builder_state_ops_are_all_implemented() -> None:
    assert set(DEV.builder.state_ops) == {"set", "append", "extend", "merge", "increment", "remove", "clear"}


@pytest.mark.parametrize(
    ("current", "op", "value", "expected"),
    [
        (1, "set", 5, 5),
        (["a"], "append", "b", ["a", "b"]),
        (None, "append", "b", ["b"]),
        ("a", "append", "b", ["a", "b"]),
        (["a"], "extend", ["b", "c"], ["a", "b", "c"]),
        (["a"], "extend", "b", ["a", "b"]),
        ({"a": {"x": 1}}, "merge", {"a": {"y": 2}, "b": 3}, {"a": {"x": 1, "y": 2}, "b": 3}),
        (None, "merge", {"b": 3}, {"b": 3}),
        (1, "increment", None, 2),
        ("2", "increment", "3", 5),
        (None, "increment", 2.5, 2.5),
        (["a", {"k": 1}, "b"], "remove", {"k": 1}, ["a", "b"]),
        ("x", "remove", None, REMOVED),
        (["a"], "clear", None, []),
        ({"a": 1}, "clear", None, {}),
        ("x", "clear", None, REMOVED),
    ],
)
def test_E12_every_state_operation(current: Any, op: str, value: Any, expected: Any) -> None:
    assert apply_op(current, op, value) == expected


def test_E12_updates_write_flow_by_top_level_key_and_see_earlier_updates() -> None:
    result = run(
        {"path": "flow.n", "op": "increment", "value": 2},
        {"path": "flow.doubled", "op": "set", "value": "{{flow.n}}"},
        {"path": "flow.profile.langs", "op": "append", "value": "de"},
        {"path": "flow.answer", "op": "set", "value": "{{nodeOutput.answer}}"},
        {"path": "flow.gone", "op": "remove"},
        {"path": "variable.tags", "op": "extend", "value": "{{nodeOutput.items}}"},
    )
    assert result.flow == {
        "n": 3,
        "doubled": 3,
        "profile": {"name": "Ada", "langs": ["en", "de"]},
        "answer": 42,
        "tags": ["a", 7, 8],
    }
    assert result.flow_removed == ["gone"]


def test_E12_the_scope_is_never_changed() -> None:
    flow = copy.deepcopy(FLOW)
    run(
        {"path": "flow.profile.langs", "op": "append", "value": "de"},
        {"path": "flow.gone", "op": "clear"},
        flow=flow,
    )
    assert flow == FLOW


def test_E12_a_false_when_skips_the_update() -> None:
    when = {"conditionLogic": "AND", "conditions": [{"field": "{{flow.n}}", "op": "gt", "value": 5}]}
    result = run(
        {"path": "flow.big", "op": "set", "value": True, "when": when},
        {"path": "flow.k", "op": "set", "value": 1},
    )
    assert result.flow == {"k": 1}


def test_E12_a_template_path_resolves_per_update_and_an_empty_one_is_skipped() -> None:
    result = run(
        {"path": "flow.{{flow.draft}}", "op": "set", "value": 1},
        {"path": "{{flow.nothing}}", "op": "set", "value": 2},
    )
    assert result.flow == {"text": 1}
    assert [w["code"] for w in result.warnings] == ["STATE_PATH_EMPTY"]


def test_E12_messages_and_threads() -> None:
    result = run(
        {"path": "messages", "op": "append", "value": "hello", "role": "user"},
        {"path": "thread.notes", "op": "extend", "value": [{"role": "system", "content": "s"}, "plain"]},
        {"path": "thread.old", "op": "clear"},
    )
    assert [(m["thread"], m["role"], m["content"]) for m in result.messages] == [
        ("messages", "user", "hello"),
        ("notes", "system", "s"),
        ("notes", "assistant", "plain"),
    ]
    assert result.cleared_threads == ["old"] and all(m["nodeId"] == "n1" for m in result.messages)


def test_E12_system_keys_are_writable_only_when_listed() -> None:
    result = run({"path": "system.userQuery", "op": "set", "value": "rewritten"}, system={"userQuery": "q"})
    assert result.system == {"userQuery": "rewritten"}
    for path in ("system.tenantId", "nodes.n1.output", "run.runId", "input.x"):
        with pytest.raises(ZonicError, match="STATE_READ_ONLY"):
            run({"path": path, "op": "set", "value": 1})


def test_E12_unknown_operations_and_message_operations_are_plan_errors() -> None:
    with pytest.raises(ZonicError, match="PLAN_INVALID"):
        run({"path": "flow.x", "op": "multiply", "value": 2})
    with pytest.raises(ZonicError, match="PLAN_INVALID"):
        run({"path": "messages", "op": "increment"})


def test_E12_json_string_literals_are_unquoted_and_missing_templates_reported() -> None:
    result = run(
        {"path": "flow.s", "op": "set", "value": '""'},
        {"path": "flow.m", "op": "set", "value": "{{flow.no}}"},
    )
    assert result.flow == {"s": "", "m": None} and result.missing == ["flow.no"]


@pytest.mark.parametrize(
    ("value", "type_", "expected"),
    [
        (5, "string", "5"),
        ({"a": 1}, "string", '{"a": 1}'),
        (None, "string", ""),
        ("4.5", "number", 4.5),
        ("7", "number", 7),
        ("x", "number", "x"),
        ("TRUE", "boolean", True),
        ("no", "boolean", False),
        ("[1, 2]", "array", [1, 2]),
        ('{"a": 1}', "object", {"a": 1}),
        ("not json", "object", "not json"),
        ({"keep": 1}, "variable", {"keep": 1}),
    ],
)
def test_E12_output_structure_types(value: Any, type_: str, expected: Any) -> None:
    assert convert_mapped(value, type_) == expected


def test_E12_output_structure_builds_named_fields() -> None:
    context = TemplateContext(scope=TemplateScope(flow={"n": "3"}), node_output={"text": "hi"})
    entries = [
        {"target": "count", "type": "number", "value": "{{flow.n}}"},
        {"target": "reply", "type": "string", "value": "{{nodeOutput.text}}"},
        {"target": "raw", "value": "{{nodeOutput}}"},
    ]
    assert build_mapping(entries, RESOLVER, context) == {"count": 3, "reply": "hi", "raw": {"text": "hi"}}


# --- F-04 --------------------------------------------------------------------------------


@pytest.mark.parametrize("op", sorted({"set", "append", "extend", "merge", "increment", "remove", "clear"}))
def test_F04_op_and_operation_spellings_give_the_same_state(op: str) -> None:
    def doc(key: str) -> dict[str, Any]:
        update = {
            "path": "flow.profile" if op == "merge" else "flow.tags",
            key: op,
            "value": {"x": 1} if op == "merge" else "b",
        }
        thread_update = {"path": "thread.t", key: "append", "role": "user", "value": "m"}
        return {
            "schemaVersion": "2.0",
            "nodes": [
                {"id": "s", "type": "start"},
                {"id": "v", "type": "variable", "config": {"variableUpdates": [update, thread_update]}},
            ],
            "edges": [{"source": "s", "target": "v"}],
        }

    loader = WorkflowLoader.from_resources(DEV)
    results = []
    for key in ("op", "operation"):
        node = loader.from_stored(doc(key)).document.node("v")
        assert node is not None
        results.append(run(*node.config["variableUpdates"]))
    assert results[0] == results[1]
    assert results[0].messages[0]["role"] == "user"
