"""Loops and limits on small documents (P1-11: E-06, E-07, E-08)."""

from __future__ import annotations

from collections import Counter
from typing import Any

import pytest
from langgraph.errors import GraphRecursionError

from tests.kinds.harness import DEFAULT_INPUT, Harness, Recorder, answer_after, policy_kinds
from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import load_resources
from zonic_runtime.engine.errors import error_code
from zonic_runtime.errors import ZonicError

DEV = load_resources(EnvironmentModel(env="dev"))
KINDS = sorted({e.kind for e in DEV.manifest.node_types.values()})


def loop_document(limits: dict[str, Any] | None = None) -> dict[str, Any]:
    """Agent -> handoff to a tool -> a variable stores the result -> back to the agent; the
    agent's answer leaves the loop to the Output."""
    document = {
        "schemaVersion": "2.0",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "agent", "type": "agent", "config": {"handoffTarget": ["tool"]}},
            {"id": "tool", "type": "variable"},
            {"id": "store", "type": "variable"},
            {"id": "out", "type": "output"},
        ],
        "edges": [
            {"source": "start", "target": "agent"},
            {"source": "agent", "target": "tool", "sourceHandle": "handoff-tool"},
            {"source": "tool", "target": "store"},
            {"source": "store", "target": "agent"},
            {"source": "agent", "target": "out"},
        ],
    }
    if limits:
        document["limits"] = limits
    return document


def harness(policy: Any) -> Harness:
    recorder = Recorder()
    return Harness(DEV, policy_kinds(KINDS, recorder, policy), recorder=recorder)


async def test_E06_a_loop_repeats_until_the_exit_is_taken_and_counts_visits() -> None:
    h = harness(answer_after(2))
    final, events, blueprint, _ = await h.run(loop_document())
    assert Counter(h.recorder.calls) == Counter({"start": 1, "agent": 3, "tool": 2, "store": 2, "out": 1})
    assert final["visits"]["agent"] == 3 and final["visits"]["__run__"] == 9
    loop = blueprint.components[0]
    iterations = [e for e in events if e["event"] == "loop.iteration"]
    assert [e["iteration"] for e in iterations] == [2, 3] and {e["loopId"] for e in iterations} == {
        loop.loop_id
    }
    assert blueprint.nodes["agent"].trace_metadata()["zonic_loop_id"] == loop.loop_id


async def test_E07_a_loop_without_an_exit_stops_at_max_visits_per_node() -> None:
    h = harness(answer_after(10**6))
    with pytest.raises(ZonicError) as caught:
        await h.run(loop_document())
    assert caught.value.code == "LOOP_LIMIT_EXCEEDED" and caught.value.details["limit"] == "maxVisitsPerNode"
    assert h.recorder.calls.count("agent") == DEV.settings.engine.max_visits_per_node


async def test_E07_the_error_policy_cannot_keep_a_run_past_its_limits() -> None:
    document = loop_document()
    document["nodes"][1]["config"]["errorHandling"] = {"policy": "continue"}
    with pytest.raises(ZonicError, match="LOOP_LIMIT_EXCEEDED"):
        await harness(answer_after(10**6)).run(document)


async def test_E08_max_steps_ends_the_run() -> None:
    h = harness(answer_after(10**6))
    with pytest.raises(ZonicError) as caught:
        await h.run(loop_document({"maxSteps": 6}))
    assert caught.value.details["limit"] == "maxSteps"
    assert len(h.recorder.calls) == 6


async def test_E08_document_limits_are_capped_by_settings() -> None:
    h = harness(answer_after(1))
    blueprint = h.plan(loop_document({"maxVisitsPerNode": 10_000, "maxSteps": 3}))
    caps = DEV.settings.engine.max_limits
    assert (
        blueprint.limits.max_visits_per_node == caps.max_visits_per_node and blueprint.limits.max_steps == 3
    )


async def test_E08_the_recursion_limit_backstop_maps_to_loop_limit_exceeded() -> None:
    h = harness(answer_after(10**6))
    blueprint = h.plan(loop_document({"maxVisitsPerNode": 50, "maxSteps": 1000}))
    graph = h.compile(blueprint)
    config = {**h.config(blueprint), "recursion_limit": 5}
    with pytest.raises(GraphRecursionError) as caught:
        await graph.ainvoke(dict(DEFAULT_INPUT), config=config, context=h.context(blueprint))
    assert error_code(caught.value) == "LOOP_LIMIT_EXCEEDED"
