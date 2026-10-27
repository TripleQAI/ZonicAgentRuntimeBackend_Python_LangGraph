"""Blueprint analysis on small documents (P1-05: D-04 to D-11, D-14, D-15, sub-flow references)."""

from __future__ import annotations

import copy
from dataclasses import replace
from typing import Any

import pytest

from tests.workflow_fixtures import SYNTHETIC, read_json
from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import RuntimeResources, load_resources
from zonic_runtime.errors import ZonicError
from zonic_runtime.models.blueprint import Blueprint
from zonic_runtime.workflow.blueprint.builder import BlueprintBuilder, BlueprintCache
from zonic_runtime.workflow.blueprint.graph import graph_name
from zonic_runtime.workflow.blueprint.subflows import subflow_closure
from zonic_runtime.workflow.loader.loader import LoadedDocument, WorkflowLoader

DEV = load_resources(EnvironmentModel(env="dev"))
LOADER = WorkflowLoader.from_resources(DEV)
BUILDER = BlueprintBuilder(DEV)


def node(node_id: str, node_type: str = "variable", **config: Any) -> dict[str, Any]:
    return {"id": node_id, "type": node_type, "name": node_id, "config": config}


def edge(source: str, target: str, handle: str | None = None) -> dict[str, Any]:
    found = {"id": f"{source}->{target}", "source": source, "target": target}
    return found | ({"sourceHandle": handle} if handle else {})


def document(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, Any]:
    return {"schemaVersion": "2.0", "agenticWorkflowId": "wf", "nodes": nodes, "edges": edges}


def rule(node_id: str, *branch_ids: str) -> dict[str, Any]:
    branches = [{"ruleId": b, "conditions": [], "conditionLogic": "AND"} for b in branch_ids]
    return node(node_id, "rule", rules=[*branches, {"ruleId": "r_else", "isDefault": True, "conditions": []}])


def loaded(doc: dict[str, Any], workflow_id: str = "wf") -> LoadedDocument:
    return LOADER.from_stored(doc, workflow_id)


def build(doc: dict[str, Any], resources: RuntimeResources = DEV) -> Blueprint:
    return BlueprintBuilder(resources).build(loaded(doc))


def failure(doc: dict[str, Any], resources: RuntimeResources = DEV) -> ZonicError:
    with pytest.raises(ZonicError) as caught:
        build(doc, resources)
    return caught.value


def codes(blueprint: Blueprint) -> list[str]:
    return [w.code for w in blueprint.warnings]


LINE = document(
    [node("start", "start"), node("a"), node("out", "output")], [edge("start", "a"), edge("a", "out")]
)


# --- D-04, D-05, D-06, D-07 ------------------------------------------------------------------


@pytest.mark.parametrize("starts", [0, 2])
def test_D04_no_entry_or_two_entries_is_plan_invalid(starts: int) -> None:
    nodes = [node(f"s{i}", "start") for i in range(starts)] + [node("a")]
    error = failure(document(nodes, [edge(n["id"], "a") for n in nodes[:-1]]))
    assert error.code == "PLAN_INVALID"
    assert [p["code"] for p in error.details["problems"]] == ["ENTRY_INVALID"]


def test_D04_edge_to_a_missing_node_is_plan_invalid() -> None:
    doc = copy.deepcopy(LINE)
    doc["edges"].append(edge("a", "ghost"))
    error = failure(doc)
    assert error.code == "PLAN_INVALID"
    assert error.details["problems"][0]["details"]["path"] == "edges[a->ghost]"


def test_D04_every_problem_is_listed_at_once() -> None:
    doc = document([node("a"), node("a")], [edge("a", "ghost")])
    problems = {p["code"] for p in failure(doc).details["problems"]}
    assert problems == {"NODE_DUPLICATE", "EDGE_UNKNOWN_NODE", "ENTRY_INVALID"}


def test_D05_unknown_node_type_is_unsupported() -> None:
    doc = copy.deepcopy(LINE)
    doc["nodes"][1]["type"] = "teleport"
    error = failure(doc)
    assert error.code == "NODE_TYPE_UNSUPPORTED"
    assert error.details["problems"][0]["details"]["type"] == "teleport"


def test_D05_unknown_types_are_skipped_when_the_manifest_says_ignore() -> None:
    manifest = DEV.manifest.model_copy(update={"on_unknown_type": "ignore"})
    doc = copy.deepcopy(LINE)
    doc["nodes"].append(node("x", "teleport"))
    doc["edges"].append(edge("a", "x"))
    blueprint = build(doc, replace(DEV, manifest=manifest))
    assert "x" not in blueprint.nodes and {"NODE_TYPE_SKIPPED", "EDGE_IGNORED_NODE"} <= set(codes(blueprint))


def test_D05_a_node_type_behind_a_feature_flag_is_unsupported_while_it_is_off() -> None:
    doc = copy.deepcopy(LINE)
    doc["nodes"][1]["type"] = "script"
    assert failure(doc).code == "FEATURE_DISABLED"
    features = DEV.settings.features.model_copy(update={"script_node": True})
    on = replace(DEV, settings=DEV.settings.model_copy(update={"features": features}))
    assert build(doc, on).nodes["a"].compiled


def test_D06_note_nodes_are_not_compiled() -> None:
    doc = copy.deepcopy(LINE)
    doc["nodes"].append(node("note_1", "note", text="remember"))
    doc["edges"].append(edge("a", "note_1"))
    blueprint = build(doc)
    assert "note_1" not in blueprint.nodes
    assert [s.node_id for s in blueprint.compiled_nodes()] == ["start", "a", "out"]
    assert "note_1" not in blueprint.edge_index.all_targets("a")


def test_D07_unreachable_node_warns_and_is_not_compiled() -> None:
    doc = copy.deepcopy(LINE)
    doc["nodes"].append(node("island"))
    blueprint = build(doc)
    assert not blueprint.nodes["island"].compiled
    assert "island" not in [s.node_id for s in blueprint.compiled_nodes()]
    assert any(w.code == "NODE_UNREACHABLE" and w.node_id == "island" for w in blueprint.warnings)


# --- D-08: handles ------------------------------------------------------------------------


def test_D08_rule_branch_without_an_edge_warns() -> None:
    doc = document(
        [node("start", "start"), rule("r", "r_a", "r_b"), node("a"), node("else")],
        [edge("start", "r"), edge("r", "a", "r_a"), edge("r", "else", "r_else")],
    )
    blueprint = build(doc)
    missing = [w for w in blueprint.warnings if w.code == "HANDLE_NO_EDGE"]
    assert [w.message.split()[1] for w in missing] == ["r_b"]
    assert set(blueprint.nodes["r"].handles) >= {"r_a", "r_b", "r_else", "r-else"}
    assert blueprint.edge_index.targets("r", "r_a") == ["a"]


def test_D08_an_edge_on_a_handle_the_node_never_activates_warns() -> None:
    doc = copy.deepcopy(LINE)
    doc["edges"][1]["sourceHandle"] = "sideways"
    assert "EDGE_UNKNOWN_HANDLE" in codes(build(doc))


def test_handoff_routing_tools_and_error_route_add_handles() -> None:
    doc = document(
        [
            node("start", "start"),
            node("agent", "agent", handoffTarget=["tool"], errorHandling={"policy": "route"}),
            node("tool", "variable"),
            node("oops"),
        ],
        [edge("start", "agent"), edge("agent", "tool", "handoff-tool"), edge("agent", "oops", "error")],
    )
    spec = build(doc).nodes["agent"]
    assert {"default", "handoff-tool", "error"} <= set(spec.handles)
    assert spec.error_policy == "route"


# --- D-09, D-10: cache ----------------------------------------------------------------------


def test_D09_same_revision_is_analysed_once_and_a_changed_one_again() -> None:
    cache = BlueprintCache(BUILDER.build, max_entries=4)
    first = cache.get(loaded(LINE))
    assert cache.get(loaded(copy.deepcopy(LINE))) is first and cache.analyses == 1
    changed = copy.deepcopy(LINE)
    changed["nodes"][1]["description"] = "edited"
    assert cache.get(loaded(changed)) is not first and cache.analyses == 2


def test_D10_cache_evicts_the_least_recently_used_revision() -> None:
    cache = BlueprintCache(BUILDER.build, max_entries=2)
    docs = []
    for i in range(3):
        doc = copy.deepcopy(LINE)
        doc["description"] = f"revision {i}"
        docs.append(loaded(doc))
    cache.get(docs[0])
    cache.get(docs[1])
    cache.get(docs[0])  # 0 is now the most recently used
    cache.get(docs[2])  # evicts 1
    assert len(cache) == 2 and cache.analyses == 3
    cache.get(docs[0])
    assert cache.analyses == 3
    cache.get(docs[1])
    assert cache.analyses == 4


# --- D-11: swarm rules --------------------------------------------------------------------


def swarm_document(**swarm: Any) -> dict[str, Any]:
    config = {"members": ["m1", "m2"], "entryPoint": "m1", "maxHandoffs": 5, "maxIterations": 5} | swarm
    return document(
        [
            node("start", "start"),
            node("sw", "swarm", **config),
            {**node("m1", "agent"), "description": "triage"},
            {**node("m2", "llm"), "description": "billing"},
            node("out", "output"),
        ],
        [edge("start", "sw"), edge("sw", "out")],
    )


def test_D11_a_valid_swarm_puts_its_members_in_the_container() -> None:
    blueprint = build(swarm_document())
    assert blueprint.nodes["m1"].container == "sw" and not blueprint.nodes["m1"].compiled
    assert [s.node_id for s in blueprint.compiled_nodes()] == ["start", "sw", "out"]


@pytest.mark.parametrize(
    ("rule_id", "change"),
    [
        ("SW-1", lambda d: d["nodes"][1]["config"].update(members=[], entryPoint="")),
        ("SW-2", lambda d: d["nodes"][1]["config"]["members"].append("out")),
        ("SW-3", lambda d: d["nodes"][3].update(name="m1")),
        ("SW-4", lambda d: d["nodes"][1]["config"].update(entryPoint="out")),
        (
            "SW-5",
            lambda d: d["nodes"].append(node("sw2", "swarm", members=["m1"], maxHandoffs=1, maxIterations=1)),
        ),
        ("SW-6", lambda d: d["edges"].append(edge("start", "m2"))),
        ("SW-7", lambda d: d["nodes"][2]["config"].update(handoffTarget=["m2"])),
        ("SW-8", lambda d: d["nodes"][1]["config"].update(maxHandoffs=0)),
    ],
)
def test_D11_swarm_errors_are_plan_invalid_naming_the_rule(rule_id: str, change: Any) -> None:
    doc = swarm_document()
    change(doc)
    error = failure(doc)
    assert rule_id in {p["details"].get("rule_id") for p in error.details["problems"]}
    assert error.code == "PLAN_INVALID"


@pytest.mark.parametrize(
    ("rule_id", "change"),
    [
        ("SW-9", lambda d: d["nodes"][3].pop("description")),
        ("SW-10", lambda d: d["nodes"][1]["config"].update(members=["m1"])),
        (
            "SW-11",
            lambda d: d["nodes"][1]["config"].update(
                repetitiveHandoffDetectionWindow=3, repetitiveHandoffMinUniqueAgents=5
            ),
        ),
        (
            "SW-12",
            lambda d: d["nodes"][1]["config"].update(nodeTimeoutSeconds=600, executionTimeoutSeconds=60),
        ),
    ],
)
def test_D11_swarm_warnings_name_the_rule(rule_id: str, change: Any) -> None:
    doc = swarm_document()
    change(doc)
    blueprint = build(doc)
    assert rule_id in {w.details.get("rule_id") for w in blueprint.warnings if w.severity == "warning"}


# --- D-14: node names -----------------------------------------------------------------------


@pytest.mark.parametrize("node_id", ["a:b", "a|b", "__start__", "__end__", "__interrupt__", "__pregel_push"])
def test_D14_reserved_ids_are_escaped_and_mapped_back(node_id: str) -> None:
    doc = document([node("start", "start"), node(node_id)], [edge("start", node_id)])
    blueprint = build(doc)
    spec = blueprint.nodes[node_id]
    assert spec.graph_name != node_id and ":" not in spec.graph_name and "|" not in spec.graph_name
    assert not spec.graph_name.startswith("__")
    assert blueprint.by_graph_name(spec.graph_name) is spec
    assert spec.trace_metadata()["zonic_node_id"] == node_id
    assert blueprint.nodes["start"].destinations == [spec.graph_name]


def test_D14_ordinary_ids_are_used_unchanged_and_escaping_is_deterministic() -> None:
    assert graph_name("rule_1") == "rule_1" and graph_name("a:b") == graph_name("a:b") != graph_name("a|b")


def test_name_index_resolves_names_and_drops_ambiguous_ones() -> None:
    doc = copy.deepcopy(LINE)
    doc["nodes"][1]["name"] = "Step"
    doc["nodes"].append({**node("b"), "name": "Twice"})
    doc["nodes"].append({**node("c"), "name": "Twice"})
    doc["edges"] += [edge("a", "b"), edge("a", "c")]
    blueprint = build(doc)
    assert (
        blueprint.resolve("Step") is blueprint.nodes["a"] and blueprint.resolve("a") is blueprint.nodes["a"]
    )
    assert "Twice" not in blueprint.name_index and "NODE_NAME_AMBIGUOUS" in codes(blueprint)


# --- D-15: joins -------------------------------------------------------------------------------


def test_D15_join_after_unequal_parallel_branches_is_a_barrier() -> None:
    blueprint = build(read_json(SYNTHETIC / "fan-in-unequal.json"))
    barriers = [s.node_id for s in blueprint.nodes.values() if s.join.barrier]
    assert barriers == ["join_1"]
    assert len(blueprint.nodes["join_1"].join.sources) == 2


def test_D15_join_after_equal_parallel_branches_is_a_barrier_too() -> None:
    doc = document(
        [node("start", "start"), node("a"), node("b"), node("j")],
        [edge("start", "a"), edge("start", "b"), edge("a", "j"), edge("b", "j")],
    )
    assert build(doc).nodes["j"].join.barrier


def test_D15_join_after_exclusive_branches_is_not_a_barrier() -> None:
    doc = document(
        [node("start", "start"), rule("r", "r_a"), node("a"), node("b"), node("j")],
        [edge("start", "r"), edge("r", "a", "r_a"), edge("r", "b", "r_else"), edge("a", "j"), edge("b", "j")],
    )
    assert not build(doc).nodes["j"].join.barrier


def test_D15_an_exclusive_branch_inside_a_parallel_branch_keeps_the_barrier() -> None:
    doc = document(
        [node("start", "start"), node("a"), rule("r", "r_a"), node("b"), node("j"), node("k")],
        [
            edge("start", "a"),
            edge("start", "b"),
            edge("a", "r"),
            edge("r", "j", "r_a"),
            edge("r", "k", "r_else"),
            edge("b", "j"),
        ],
    )
    blueprint = build(doc)
    assert blueprint.nodes["j"].join.barrier and not blueprint.nodes["k"].join.barrier


@pytest.mark.parametrize(("policy", "barrier"), [("any", False), ("barrier", True), ("auto", True)])
def test_D15_join_policy_overrides_the_analysis(policy: str, barrier: bool) -> None:
    doc = read_json(SYNTHETIC / "fan-in-unequal.json")
    join = next(n for n in doc["nodes"] if n["id"] == "join_1")
    join["properties"] = {"joinPolicy": policy}
    spec = build(doc).nodes["join_1"]
    assert spec.join.policy == policy and spec.join.barrier is barrier


def test_D15_barrier_policy_on_a_single_path_node() -> None:
    doc = copy.deepcopy(LINE)
    doc["nodes"][1]["properties"] = {"joinPolicy": "barrier"}
    assert build(doc).nodes["a"].join.barrier


# --- loops (design 6.6) -----------------------------------------------------------------------


def test_cycles_become_components_with_loop_ids_on_their_nodes() -> None:
    blueprint = build(read_json(SYNTHETIC / "supervisor-loop.json"))
    assert len(blueprint.components) == 1
    loop = blueprint.components[0]
    assert loop.has_exit and loop.entries
    assert all(blueprint.nodes[n].loop_id == loop.loop_id for n in loop.nodes)
    assert blueprint.nodes[loop.nodes[0]].trace_metadata()["zonic_loop_id"] == loop.loop_id


def test_a_loop_without_an_exit_warns() -> None:
    doc = document(
        [node("start", "start"), node("a"), node("b")],
        [edge("start", "a"), edge("a", "b"), edge("b", "a")],
    )
    blueprint = build(doc)
    assert "LOOP_NO_EXIT" in codes(blueprint) and not blueprint.components[0].has_exit


def test_a_self_loop_is_a_component() -> None:
    doc = document(
        [node("start", "start"), node("a"), node("out", "output")],
        [edge("start", "a"), edge("a", "a"), edge("a", "out")],
    )
    assert [c.nodes for c in build(doc).components] == [["a"]]


# --- node specs ------------------------------------------------------------------------------


def test_specs_take_timeouts_and_policies_from_the_node_then_the_manifest() -> None:
    doc = copy.deepcopy(LINE)
    doc["nodes"][1]["config"] = {"timeoutSeconds": 7}
    blueprint = build(doc)
    assert blueprint.nodes["a"].timeout_seconds == 7
    assert blueprint.nodes["out"].timeout_seconds == DEV.manifest.defaults.timeout_seconds
    assert blueprint.nodes["out"].terminal and blueprint.nodes["a"].error_policy == "fail_run"
    assert blueprint.nodes["a"].retry == DEV.manifest.defaults.retry


def test_invalid_updates_fail_at_plan_time() -> None:
    doc = copy.deepcopy(LINE)
    doc["nodes"][1]["config"] = {"variableUpdates": [{"path": "flow.x", "op": "multiply", "value": 2}]}
    error = failure(doc)
    assert [p["code"] for p in error.details["problems"]] == ["UPDATE_INVALID"]
    assert error.details["problems"][0]["details"]["path"] == "nodes[a].config.variableUpdates[0]"


def test_normalizer_errors_fail_with_their_own_code() -> None:
    error = failure(read_json(SYNTHETIC / "rule-expression.json"))
    assert error.code == "RULE_EXPRESSION_UNSUPPORTED" and len(error.details["problems"]) == 2


# --- sub-flow references ------------------------------------------------------------------------


def chain() -> dict[str, Blueprint]:
    names = ("subflow-chain-parent", "subflow-chain-child", "subflow-chain-grandchild")
    found = {}
    for name in names:
        doc = read_json(SYNTHETIC / f"{name}.json")
        blueprint = BUILDER.build(loaded(doc, doc["agenticWorkflowId"]))
        found[blueprint.workflow_id] = blueprint
    return found


async def test_subflow_closure_loads_each_level_once() -> None:
    known = chain()
    calls: list[list[str]] = []

    async def load(ids: Any) -> list[Blueprint]:
        calls.append(list(ids))
        return [known[i] for i in ids]

    root = known["synthetic-subflow-chain-parent"]
    assert root.children == ["synthetic-subflow-chain-child"]
    closure = await subflow_closure(root, load, max_depth=5)
    assert set(closure) == set(known)
    assert calls == [["synthetic-subflow-chain-child"], ["synthetic-subflow-chain-grandchild"]]


async def test_subflow_cycle_and_depth_fail_before_running() -> None:
    known = chain()
    grandchild = known["synthetic-subflow-chain-grandchild"]
    known[grandchild.workflow_id] = grandchild.model_copy(
        update={"children": ["synthetic-subflow-chain-parent"]}
    )

    async def load(ids: Any) -> list[Blueprint]:
        return [known[i] for i in ids]

    with pytest.raises(ZonicError) as caught:
        await subflow_closure(known["synthetic-subflow-chain-parent"], load, max_depth=5)
    assert caught.value.code == "SUBFLOW_CYCLE"
    assert caught.value.details["path"][0] == caught.value.details["path"][-1]

    with pytest.raises(ZonicError, match="SUBFLOW_DEPTH_EXCEEDED"):
        await subflow_closure(chain()["synthetic-subflow-chain-parent"], load, max_depth=1)
