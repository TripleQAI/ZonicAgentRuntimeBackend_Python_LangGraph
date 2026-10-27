"""The vendored loop documents through the node template (P1-11: E-09, F-06)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from tests.kinds.harness import Harness, Recorder, answer_after, policy_kinds, seek_loop
from tests.workflow_fixtures import BUILDER, SYNTHETIC, read_json
from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import load_resources
from zonic_runtime.errors import ZonicError

DEV = load_resources(EnvironmentModel(env="dev"))
RESOURCES = replace(
    DEV,
    settings=DEV.settings.model_copy(
        update={"features": DEV.settings.features.model_copy(update={"script_node": True})}
    ),
)
KINDS = sorted({e.kind for e in RESOURCES.manifest.node_types.values()})
PLATFORM = sorted((BUILDER / "platform").glob("*.json"))
SYNTHETIC_LOOPS = [
    SYNTHETIC / f"{name}.json" for name in ("approval-in-loop", "nested-loops", "supervisor-loop")
]


def harness(policy: object) -> Harness:
    recorder = Recorder()
    return Harness(RESOURCES, policy_kinds(KINDS, recorder, policy), recorder=recorder)  # type: ignore[arg-type]


def loops_of(path: Path) -> int:
    return len(harness(answer_after(1)).plan(read_json(path)).components)


LOOPED = [p for p in PLATFORM + SYNTHETIC_LOOPS if loops_of(p) > 0]


def test_F06_the_platform_and_synthetic_loop_documents() -> None:
    assert len([p for p in LOOPED if p.parent.name == "platform"]) == 8
    assert len(LOOPED) == 11


@pytest.mark.parametrize("path", LOOPED, ids=lambda p: p.stem)
async def test_F06_loops_run_until_the_agent_answers(path: Path) -> None:
    h = harness(answer_after(2))
    final, _, blueprint, _ = await h.run(read_json(path))
    assert final["visits"]["__run__"] == len(h.recorder.calls)
    assert all(
        v <= blueprint.limits.max_visits_per_node for k, v in final["visits"].items() if k != "__run__"
    )


@pytest.mark.parametrize("path", [p for p in LOOPED if p.parent.name == "platform"], ids=lambda p: p.stem)
async def test_F06_an_agent_that_always_hands_off_stops_at_the_visit_limit(path: Path) -> None:
    h = harness(seek_loop())
    with pytest.raises(ZonicError) as caught:
        await h.run(read_json(path))
    assert caught.value.code == "LOOP_LIMIT_EXCEEDED"
    agents = {s.node_id for s in h.plan(read_json(path)).nodes.values() if s.kind == "agent"}
    calls = [c for c in h.recorder.calls if c in agents]
    assert calls and max(calls.count(a) for a in agents) <= DEV.settings.engine.max_visits_per_node


async def test_E09_the_loop_with_several_entries_runs_and_exits() -> None:
    path = BUILDER / "platform" / "intelligent-buying-orchestrator.json"
    h = harness(answer_after(3))
    _, events, blueprint, _ = await h.run(read_json(path))
    loop = blueprint.components[0]
    assert len(loop.entries) >= 2 and len(loop.nodes) >= 10
    entered = [n for n in loop.entries if n in h.recorder.calls]
    assert entered
    assert any(e["event"] == "loop.iteration" and e["loopId"] == loop.loop_id for e in events)


async def test_F06_all_agents_finishes_after_one_sub_flow() -> None:
    h = harness(answer_after(1))
    final, _, blueprint, _ = await h.run(read_json(BUILDER / "zonic" / "all-agents.json"))
    subflows = [s.node_id for s in blueprint.nodes.values() if s.kind == "subflow"]
    assert len(subflows) == 3 and sum(h.recorder.calls.count(s) for s in subflows) == 1
    assert final["visits"]["__run__"] == len(h.recorder.calls)
