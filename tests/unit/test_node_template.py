"""Node template, error policy, result and child graphs (P1-10: E-13 to E-16, E-18, G-14)."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.graph import START, StateGraph
from langgraph.runtime import Runtime

from tests.conformance.test_kinds import doc, failing, fast_retry, harness, llm
from tests.kinds.harness import ChildKind, EchoKind, FailingKind, Harness, Recorder, ResultKind, SlowKind
from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import load_resources
from zonic_runtime.engine.context import RunContext
from zonic_runtime.engine.result import FROM_OUTPUT_NODE, run_result
from zonic_runtime.engine.state import RunState
from zonic_runtime.errors import ZonicError

DEV = load_resources(EnvironmentModel(env="dev"))


# --- E-13: timeouts -----------------------------------------------------------------------


async def test_E13_a_timed_out_attempt_writes_nothing_and_the_policy_applies() -> None:
    h = harness(lambda s, b, r: SlowKind(s, b, r, 5), "variable")
    target = {
        "id": "n",
        "type": "variable",
        "config": {
            "timeoutSeconds": 0.05,
            "errorHandling": {"policy": "continue"},
            "variableUpdates": [{"path": "flow.touched", "value": True}],
        },
    }
    final, *_ = await h.run(
        doc(target, "one", [{"id": "ok", "type": "output"}], [{"source": "n", "target": "ok"}])
    )
    assert (
        final["nodes"]["n"]["status"] == "failed" and final["nodes"]["n"]["error"]["code"] == "NODE_TIMEOUT"
    )
    assert "touched" not in final.get("flow", {}).get("values", {})
    assert "ok" in final["nodes"] and [e["code"] for e in final["errors"]] == ["NODE_TIMEOUT"]


# --- E-14, E-15: continue and route ----------------------------------------------------------


@pytest.mark.parametrize(("policy", "taken"), [("continue", "ok"), ("route", "err")])
async def test_E14_continue_follows_default_and_route_follows_error(policy: str, taken: str) -> None:
    h, document = failing(policy)
    final, *_ = await h.run(document)
    assert taken in final["nodes"] and final["errors"][0]["policy"] == policy


async def test_E15_continue_in_one_parallel_branch_keeps_the_sibling_updates() -> None:
    # Also the regression test for the LangGraph 1.2.17 defect fixed in 1.2.18: a handled error
    # in a parallel superstep, or while the custom stream is on, was re-raised at the end.
    recorder = Recorder()
    kinds = {
        "variable": lambda s, b: (
            FailingKind(s, b, recorder, "TOOL_UNAVAILABLE") if s.node_id == "a" else EchoKind(s, b, recorder)
        )
    }
    h = Harness(DEV, kinds, recorder=recorder)
    document = {
        "schemaVersion": "2.0",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "a", "type": "variable", "config": {"errorHandling": {"policy": "continue"}}},
            {
                "id": "b",
                "type": "variable",
                "config": {"variableUpdates": [{"path": "flow.fromB", "value": 1}]},
            },
        ],
        "edges": [{"source": "start", "target": "a"}, {"source": "start", "target": "b"}],
    }
    final, events, *_ = await h.run(document)
    assert final["flow"]["values"] == {"fromB": 1}
    assert final["nodes"]["a"]["status"] == "failed" and final["nodes"]["b"]["status"] == "succeeded"
    assert any(e["event"] == "node.failed" and e["nodeId"] == "a" for e in events)


# --- E-16: retries ---------------------------------------------------------------------------


@pytest.mark.parametrize(("code", "attempts"), [("MODEL_UNAVAILABLE", 3), ("TOOL_NOT_ALLOWED", 1)])
async def test_E16_retry_for_retryable_codes_only(code: str, attempts: int) -> None:
    h = harness(lambda s, b, r: FailingKind(s, b, r, code), "llm", fast_retry())
    with pytest.raises(ZonicError, match=code):
        await h.run(doc(llm(), "one"))
    assert h.recorder.attempts["n"] == attempts


# --- E-18: the run result ------------------------------------------------------------------------


async def test_E18_two_outputs_on_different_branches_first_wins_second_warns() -> None:
    recorder = Recorder()
    h = Harness(DEV, {"output": lambda s, b: ResultKind(s, b, recorder)}, recorder=recorder)
    document = {
        "schemaVersion": "2.0",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "slow", "type": "variable"},
            {"id": "out_a", "type": "output"},
            {"id": "out_b", "type": "output"},
        ],
        "edges": [
            {"source": "start", "target": "out_a"},
            {"source": "start", "target": "slow"},
            {"source": "slow", "target": "out_b"},
        ],
    }
    final, _, blueprint, _ = await h.run(document)
    result = run_result(final, blueprint, DEV.settings.result.fallback)
    assert result.source == FROM_OUTPUT_NODE and result.value == {"answer": "out_a"}
    assert result.warnings == [{"code": "OUTPUT_IGNORED", "nodeId": "out_b", "kept": "out_a"}]


async def test_E18_without_an_output_the_fallback_or_no_output() -> None:
    h = harness(lambda s, b, r: EchoKind(s, b, r, raw={"text": "llm said"}))
    final, _, blueprint, _ = await h.run(
        doc(llm(), "one"), {"flow": {"values": {"topic": "t"}, "provenance": {}, "conflicts": []}}
    )
    result = run_result(final, blueprint, DEV.settings.result.fallback)
    assert (result.source, result.value) == ("last_llm_text", {"text": "llm said"})
    with pytest.raises(ZonicError, match="NO_OUTPUT"):
        run_result(final, blueprint, [])


# --- G-14 (DR-6): child graphs -----------------------------------------------------------------


async def test_G14_invoke_child_passes_the_run_context_and_config() -> None:
    def child_node(state: RunState, config: RunnableConfig, runtime: Runtime[RunContext]) -> dict[str, Any]:
        seen = {
            "runId": runtime.context.run.run_id,
            "tags": sorted(config.get("tags", [])),
            "limit": config.get("recursion_limit"),
        }
        return {"nodes": {"child": {"visit": 1, "seen": seen}}}

    child_builder: StateGraph[Any, Any, Any, Any] = StateGraph(RunState, context_schema=RunContext)
    child_builder.add_node("child", child_node)
    child_builder.add_edge(START, "child")
    child_graph = child_builder.compile()

    class Seen(ChildKind):
        async def execute(self, ctx: Any, node_input: Any) -> Any:
            from zonic_runtime.engine.child import invoke_child
            from zonic_runtime.models.engine import NodeOutcome

            final = await invoke_child(self.child_graph, {}, ctx.runtime, ctx.config)
            return NodeOutcome(output={"result": final["nodes"]["child"]["seen"]}, activate=["default"])

    recorder = Recorder()
    h = Harness(DEV, {"subflow": lambda s, b: Seen(s, b, recorder, child_graph)}, recorder=recorder)
    document = {
        "schemaVersion": "2.0",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "sub", "type": "workflow", "config": {"agenticWorkflowId": "child-wf"}},
        ],
        "edges": [{"source": "start", "target": "sub"}],
    }
    final, *_ = await h.run(document, run_id="run-child")
    seen = final["nodes"]["sub"]["output"]
    assert seen["runId"] == "run-child"
    assert "workflow:wf" in seen["tags"] and seen["limit"] == DEV.settings.engine.child_supersteps


def test_G14_the_principle_check_covers_kinds() -> None:
    from tests.principles.test_principles import GRAPH_WORDS

    assert {"graph", "agent", "swarm", "subflow", "child"} <= set(GRAPH_WORDS)


async def test_an_unregistered_kind_cannot_be_compiled() -> None:
    from zonic_runtime.engine.kinds.registry import KindRegistry

    blueprint = harness(lambda s, b, r: None).plan(doc(llm(), "one"))
    with pytest.raises(ZonicError, match="NODE_TYPE_UNSUPPORTED"):
        KindRegistry({}).create(blueprint.nodes["n"], blueprint)


async def test_write_to_also_writes_the_output_at_the_named_path() -> None:
    h = harness(lambda s, b, r: EchoKind(s, b, r, raw={"result": {"report": "done"}}), "output")
    target = {"id": "n", "type": "output", "config": {"outputKey": "flow.report"}}
    final, *_ = await h.run(doc(target, "one"))
    assert final["flow"]["values"]["report"] == {"report": "done"}
