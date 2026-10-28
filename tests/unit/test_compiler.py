"""Compiler, routing and graph cache (P1-09: E-01 to E-05, E-20, G-02 to G-04, G-07, G-09 to G-13, D-09)."""

from __future__ import annotations

import asyncio
import copy
import gc
from collections import Counter
from dataclasses import replace
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START

from tests.kinds.reference import interpret
from tests.kinds.scripted_kind import ScriptedRuns, run_context
from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import RuntimeResources, load_resources
from zonic_runtime.engine.context import RunContext, RunIdentity
from zonic_runtime.engine.invocation import invocation_config
from zonic_runtime.engine.routing import targets
from zonic_runtime.errors import ZonicError
from zonic_runtime.models.blueprint import Blueprint
from zonic_runtime.models.resources.manifest import NodeRuntimeManifest
from zonic_runtime.workflow.blueprint.builder import BlueprintBuilder, BlueprintCache
from zonic_runtime.workflow.compiler.graph_cache import GraphCache
from zonic_runtime.workflow.compiler.workflow_compiler import WorkflowCompiler
from zonic_runtime.workflow.loader.loader import WorkflowLoader

DEV = load_resources(EnvironmentModel(env="dev"))


def plan(doc: dict[str, Any], resources: RuntimeResources = DEV) -> Blueprint:
    return BlueprintBuilder(resources).build(WorkflowLoader.from_resources(resources).from_stored(doc, "wf"))


def node(node_id: str, node_type: str = "variable", **config: Any) -> dict[str, Any]:
    return {"id": node_id, "type": node_type, "name": node_id, "config": config}


def rule(node_id: str, *branches: str) -> dict[str, Any]:
    rules = [{"ruleId": b, "conditions": [{"field": "1", "op": "eq", "value": 1}]} for b in branches]
    return node(node_id, "rule", rules=[*rules, {"ruleId": "r_else", "isDefault": True, "conditions": []}])


def edge(source: str, target: str, handle: str | None = None) -> dict[str, Any]:
    return {"source": source, "target": target} | ({"sourceHandle": handle} if handle else {})


def document(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schemaVersion": "2.0",
        "agenticWorkflowId": "wf",
        "nodes": [node("start", "start"), *nodes],
        "edges": edges,
    }


async def run(
    blueprint: Blueprint, runs: ScriptedRuns, run_id: str = "run-1", graph: Any = None
) -> dict[str, Any]:
    graph = graph or WorkflowCompiler(runs.factory, DEV.settings.engine).compile(blueprint, role="child")
    context = run_context(blueprint, DEV, run_id)
    config = invocation_config(context.run, DEV.settings.engine, environment="test", child=True)
    result: dict[str, Any] = await graph.ainvoke({}, config=config, context=context)
    return result


def steps(runs: ScriptedRuns) -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for execution in runs.log:
        found.setdefault(execution.node_id, []).append(execution.step)
    return found


# --- E-01 to E-05, E-20: routing ------------------------------------------------------------------


async def test_E01_a_linear_chain_runs_every_node_once_in_order() -> None:
    blueprint = plan(
        document(
            [node("a"), node("b"), node("out", "output")],
            [edge("start", "a"), edge("a", "b"), edge("b", "out")],
        )
    )
    runs = ScriptedRuns()
    final = await run(blueprint, runs)
    assert runs.executed() == ["start", "a", "b", "out"]
    assert [e.step for e in runs.log] == sorted(e.step for e in runs.log) and len(
        {e.step for e in runs.log}
    ) == 4
    assert final["visits"] == {"start": 1, "a": 1, "b": 1, "out": 1, "__run__": 4}


async def test_E02_a_fan_out_runs_both_branches_in_one_superstep() -> None:
    blueprint = plan(document([node("a"), node("b")], [edge("start", "a"), edge("start", "b")]))
    runs = ScriptedRuns()
    await run(blueprint, runs)
    found = steps(runs)
    assert found["a"] == found["b"] and len(found["a"]) == 1


async def test_E03_exclusive_branches_join_once_and_the_other_branch_never_runs() -> None:
    blueprint = plan(
        document(
            [rule("r", "r_a"), node("a"), node("b"), node("j"), node("out", "output")],
            [
                edge("start", "r"),
                edge("r", "a", "r_a"),
                edge("r", "b", "r_else"),
                edge("a", "j"),
                edge("b", "j"),
                edge("j", "out"),
            ],
        )
    )
    assert not blueprint.nodes["j"].join.barrier
    runs = ScriptedRuns(by_node={"r": ["r_a"]})
    final = await run(blueprint, runs)
    assert runs.executed() == ["start", "r", "a", "j", "out"]
    assert "b" not in final["nodes"]


UNEQUAL = document(
    [node("a"), node("b1"), node("b2"), node("j"), node("out", "output")],
    [
        edge("start", "a"),
        edge("start", "b1"),
        edge("b1", "b2"),
        edge("a", "j"),
        edge("b2", "j"),
        edge("j", "out"),
    ],
)


async def test_E04_unequal_parallel_branches_join_once_with_both_outputs() -> None:
    blueprint = plan(UNEQUAL)
    assert blueprint.nodes["j"].join.barrier
    runs = ScriptedRuns()
    await run(blueprint, runs)
    joins = [e for e in runs.log if e.node_id == "j"]
    assert len(joins) == 1 and {"a", "b2"} <= set(joins[0].seen)


async def test_E05_join_policy_any_runs_once_per_arriving_branch() -> None:
    doc = copy.deepcopy(UNEQUAL)
    next(n for n in doc["nodes"] if n["id"] == "j")["properties"] = {"joinPolicy": "any"}
    blueprint = plan(doc)
    runs = ScriptedRuns()
    await run(blueprint, runs)
    assert runs.executed().count("j") == 2 and runs.executed().count("out") == 2


async def test_E20_two_commands_to_one_node_in_one_superstep_run_it_once() -> None:
    blueprint = plan(
        document(
            [node("a"), node("b"), node("j")],
            [edge("start", "a"), edge("start", "b"), edge("a", "j"), edge("b", "j")],
        )
    )
    # Equal branches would also be barriers; without one, both arrivals land in one superstep.
    doc_join = blueprint.nodes["j"].model_copy(
        update={"join": blueprint.nodes["j"].join.model_copy(update={"barrier": False})}
    )
    unbarred = blueprint.model_copy(update={"nodes": {**blueprint.nodes, "j": doc_join}})
    runs = ScriptedRuns()
    await run(unbarred, runs)
    assert runs.executed().count("j") == 1


async def test_route_unresolved_only_when_an_unconnected_branch_is_taken() -> None:
    blueprint = plan(
        document(
            [rule("r", "r_a", "r_b"), node("a"), node("e")],
            [edge("start", "r"), edge("r", "a", "r_a"), edge("r", "e", "r_else")],
        )
    )
    await run(blueprint, ScriptedRuns(by_node={"r": ["r_a"]}))
    with pytest.raises(ZonicError) as caught:
        await run(blueprint, ScriptedRuns(by_node={"r": ["r_b"]}))
    assert caught.value.code == "ROUTE_UNRESOLVED" and caught.value.details == {
        "node_id": "r",
        "handle": "r_b",
    }


def test_optional_handles_and_sinks_end_the_path() -> None:
    blueprint = plan(
        document([node("llm_1", "llm"), node("a")], [edge("start", "llm_1"), edge("llm_1", "a")])
    )
    assert targets(blueprint, blueprint.nodes["llm_1"], ["error"]) == []
    assert targets(blueprint, blueprint.nodes["a"], ["default"]) == []
    assert targets(blueprint, blueprint.nodes["start"], ["*"]) == ["llm_1"]


# --- G-02 to G-04 ------------------------------------------------------------------------------------


def compiled(doc: dict[str, Any] = UNEQUAL) -> tuple[Blueprint, Any]:
    blueprint = plan(doc)
    return blueprint, WorkflowCompiler(ScriptedRuns().factory, DEV.settings.engine).compile(
        blueprint, role="child"
    )


def test_G02_the_only_static_edge_is_start_to_the_entry() -> None:
    _, graph = compiled()
    assert graph.builder.edges == {(START, "start")}


def test_G03_destinations_equal_the_edge_targets_and_render() -> None:
    blueprint, graph = compiled()
    for spec in blueprint.compiled_nodes():
        declared = graph.builder.nodes[spec.graph_name].ends or ()
        assert set(declared) == set(spec.destinations), spec.node_id
    drawn = {(e.source, e.target) for e in graph.get_graph().edges}
    assert {("start", "a"), ("start", "b1"), ("b1", "b2"), ("a", "j"), ("b2", "j"), ("j", "out")} <= drawn


def test_G04_node_policies_match_the_manifest_and_the_blueprint() -> None:
    doc = document(
        [node("llm_1", "llm", messages=[{"role": "user", "content": "x"}], timeoutSeconds=7), node("a")],
        [edge("start", "llm_1"), edge("llm_1", "a")],
    )
    blueprint, graph = compiled(doc)
    llm = graph.builder.nodes["llm_1"]
    retry = DEV.manifest.node_types["llm"].retry
    assert retry is not None and llm.retry_policy.max_attempts == retry.max_attempts
    assert llm.retry_policy.retry_on(ZonicError("MODEL_BUSY")) and not llm.retry_policy.retry_on(
        ZonicError("INPUT_INVALID")
    )
    assert not llm.retry_policy.retry_on(ValueError("x"))
    assert llm.timeout.run_timeout == 7
    assert graph.builder.nodes["a"].retry_policy is None
    assert graph.builder.nodes["a"].timeout.run_timeout == DEV.manifest.defaults.timeout_seconds
    assert llm.metadata == blueprint.nodes["llm_1"].trace_metadata()
    _, joined = compiled()
    assert joined.builder.nodes["j"].defer and not joined.builder.nodes["a"].defer


# --- G-07, G-11, G-12, G-13, D-09: sharing and caching ------------------------------------------------


async def test_G07_concurrent_runs_share_the_graph_and_nothing_else() -> None:
    blueprint = plan(UNEQUAL)
    runs = ScriptedRuns()
    graph = WorkflowCompiler(runs.factory, DEV.settings.engine).compile(blueprint, role="child")
    finals = await asyncio.gather(*(run(blueprint, runs, f"run-{i}", graph) for i in range(4)))
    for i, final in enumerate(finals):
        assert Counter(runs.executed(f"run-{i}")) == Counter(
            {"start": 1, "a": 1, "b1": 1, "b2": 1, "j": 1, "out": 1}
        )
        assert final["visits"]["__run__"] == 6


def _reaches(root: Any, kind: type, limit: int = 200_000) -> bool:
    seen: set[int] = set()
    stack = [root]
    while stack and len(seen) < limit:
        item = stack.pop()
        if id(item) in seen or isinstance(item, str | bytes | int | float | bool | type(None)):
            continue
        seen.add(id(item))
        if isinstance(item, kind):
            return True
        if isinstance(item, dict):
            stack += list(item.keys()) + list(item.values())
        elif isinstance(item, list | tuple | set | frozenset):
            stack += list(item)
        else:
            stack += gc.get_referents(item)
    return False


async def test_G11_a_compiled_graph_holds_no_run_context_after_runs() -> None:
    blueprint = plan(UNEQUAL)
    runs = ScriptedRuns()
    graph = WorkflowCompiler(runs.factory, DEV.settings.engine).compile(blueprint, role="child")
    await run(blueprint, runs, "run-x", graph)
    assert not _reaches(graph, RunContext)
    assert _reaches({"probe": run_context(blueprint, DEV)}, RunContext)  # the inspection does find one


def test_G12_invocation_config_comes_from_settings() -> None:
    identity = RunIdentity("r1", "s1", "owner", None, "wf", "rev", mode="sync")
    engine = DEV.settings.engine
    root = invocation_config(identity, engine, environment="dev")
    assert root["configurable"] == {"thread_id": "r1"}
    assert (root["recursion_limit"], root["max_concurrency"]) == (
        engine.supersteps,
        engine.max_concurrency_per_graph,
    )
    assert (
        invocation_config(identity, engine, environment="dev", child=True)["recursion_limit"]
        == engine.child_supersteps
    )
    assert root["metadata"]["zonic_revision"] == "rev" and "mode:sync" in root["tags"]


def test_G13_root_and_child_are_cached_and_compiled_separately() -> None:
    saver = InMemorySaver()
    compiler = WorkflowCompiler(ScriptedRuns().factory, DEV.settings.engine, saver=saver)
    cache = GraphCache(lambda b, role: compiler.compile(b, role=role), DEV.manifest, max_entries=8)
    blueprint = plan(UNEQUAL)
    root, child = cache.get(blueprint, "root"), cache.get(blueprint, "child")
    assert root is not child and root.checkpointer is saver and child.checkpointer is None
    assert cache.get(blueprint, "root") is root and cache.compilations == 2
    assert cache.get(blueprint, "root", tool_catalog_version="2") is not root and cache.compilations == 3


def test_D09_the_same_document_plans_and_compiles_once_and_a_change_again() -> None:
    loader = WorkflowLoader.from_resources(DEV)
    blueprints = BlueprintCache(BlueprintBuilder(DEV).build, 8)
    compiler = WorkflowCompiler(ScriptedRuns().factory, DEV.settings.engine)
    graphs = GraphCache(lambda b, role: compiler.compile(b, role=role), DEV.manifest, 8)
    for _ in range(3):
        graphs.get(blueprints.get(loader.from_stored(copy.deepcopy(UNEQUAL), "wf")))
    changed = copy.deepcopy(UNEQUAL)
    changed["nodes"][1]["description"] = "edited"
    graphs.get(blueprints.get(loader.from_stored(changed, "wf")))
    assert (blueprints.analyses, graphs.compilations) == (2, 2)


def test_G13_a_manifest_change_changes_the_key() -> None:
    data = DEV.manifest.model_dump(by_alias=True, exclude_none=True)
    data["defaults"]["timeoutSeconds"] = 99
    other = NodeRuntimeManifest.model_validate(data)
    blueprint = plan(UNEQUAL)
    first = GraphCache(lambda b, r: object(), DEV.manifest, 2).key(blueprint, "root")
    second = GraphCache(lambda b, r: object(), other, 2).key(blueprint, "root")
    assert first.manifest_version != second.manifest_version


# --- G-10: a new node type ---------------------------------------------------------------------------


async def test_G10_a_new_node_type_by_manifest_entry_only_compiles_and_runs() -> None:
    data = DEV.manifest.model_dump(by_alias=True, exclude_none=True)
    data["nodeTypes"]["checkpoint_note"] = copy.deepcopy(data["nodeTypes"]["variable"])
    resources = replace(DEV, manifest=NodeRuntimeManifest.model_validate(data))
    blueprint = plan(
        document(
            [node("c", "checkpoint_note"), node("out", "output")], [edge("start", "c"), edge("c", "out")]
        ),
        resources,
    )
    runs = ScriptedRuns()
    await run(blueprint, runs)
    assert runs.executed() == ["start", "c", "out"]


# --- G-09: against the reference interpreter -------------------------------------------------------


@st.composite
def random_documents(draw: Any) -> tuple[dict[str, Any], dict[str, list[str]]]:
    size = draw(st.integers(min_value=2, max_value=9))
    ids = [f"n{i}" for i in range(1, size + 1)]
    kinds = {i: draw(st.sampled_from(["variable", "variable", "rule", "output"])) for i in ids}
    kinds[ids[-1]] = "output"
    nodes, edges, choices = [], [edge("start", ids[0])], {}
    for position, node_id in enumerate(ids):
        later = ids[position + 1 :]
        if kinds[node_id] == "rule":
            nodes.append(rule(node_id, "r_a"))
            for handle in ("r_a", "r_else"):
                if later:
                    edges.append(edge(node_id, draw(st.sampled_from(later)), handle))
            choices[node_id] = [draw(st.sampled_from(["r_a", "r_else"]))]
        elif kinds[node_id] == "output":
            nodes.append(node(node_id, "output"))
        else:
            nodes.append(node(node_id))
            for target in draw(st.lists(st.sampled_from(later), max_size=3, unique=True)) if later else []:
                edges.append(edge(node_id, target))
        if position == 0 and kinds[node_id] == "output" and later:
            kinds[node_id] = "variable"
    return document(nodes, edges), choices


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(random_documents())
async def test_G09_executed_nodes_equal_the_reference_interpreter(
    case: tuple[dict[str, Any], dict[str, list[str]]],
) -> None:
    doc, choices = case
    blueprint = plan(doc)
    runs = ScriptedRuns(by_node=choices)
    await run(blueprint, runs)

    def activate(spec: Any, visit: int) -> list[str]:
        return choices.get(spec.node_id) or ([] if spec.terminal else ["default"])

    assert Counter(runs.executed()) == interpret(blueprint, activate)
