"""Builder scenarios end to end through the app (P2-09: F-07 and the coverage of Section 20.4).

Each scenario runs every turn in stream mode and again in sync mode against the scripted model:
both take the scenario's path and give the same result, and the stream matches its golden file
under ``tests/fixtures/scenarios/`` (volatile fields removed). Regenerate after an intended change::

    ZONIC_UPDATE_SNAPSHOTS=1 uv run pytest tests/scenarios
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.api.app import DEV, auth, make_app, sse_events
from tests.fakes.egress import FakeEgress
from tests.fakes.mcp_server import FakeMcpServer, ServerToolsMcp, ok
from tests.fakes.recordings import Recordings
from tests.scenarios.builder.scenarios import FIXTURE_GAPS, SCENARIOS, WAITING, Scenario
from tests.unit.test_sandbox import FakePiston
from tests.workflow_fixtures import (
    BUILDER,
    ROOT,
    SYNTHETIC,
    document_files,
    fixture_id,
    load_document,
    read_json,
)
from zonic_runtime.config.settings import RuntimeResources
from zonic_runtime.engine.kinds.core import core_kinds
from zonic_runtime.engine.node_template import ServicePorts
from zonic_runtime.errors import ZonicError
from zonic_runtime.integrations.mcp.execute import McpService
from zonic_runtime.workflow.blueprint.builder import BlueprintBuilder
from zonic_runtime.workflow.loader.loader import WorkflowLoader

GOLDEN = ROOT / "tests" / "fixtures" / "scenarios"
KIND_NAMES = frozenset(e.kind for e in DEV.manifest.node_types.values())
PROGRESS = ROOT / "tests" / "progress.yaml"
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
VOLATILE = frozenset(
    {"id", "runId", "ts", "durationMs", "startedAt", "finishedAt", "traceId", "revision", "expiresAt"}
)
RUNNABLE = [s for s in SCENARIOS if s.turns]
#: Model calls a responder scenario may make in one run.
RESPONSES = 200


def enabled(resources: RuntimeResources) -> RuntimeResources:
    """Every catalog model enabled (the scripted model answers for all of them) and the script
    node's feature on, as for the compiled-graph snapshots, so every fixture plans."""
    models = {c: e.model_copy(update={"enabled": True}) for c, e in resources.models.models.items()}
    features = resources.settings.features.model_copy(update={"script_node": True})
    return replace(
        resources,
        models=resources.models.model_copy(update={"models": models}),
        settings=resources.settings.model_copy(update={"features": features}),
    )


RESOURCES = enabled(DEV)
LOADER = WorkflowLoader.from_resources(RESOURCES)
PLANNER = BlueprintBuilder(RESOURCES)


def fixture_path(scenario: Scenario) -> Path:
    return BUILDER / f"{scenario.fixture}.json"


def workflow_id(scenario: Scenario) -> str:
    """A stable UUID per fixture: the catalog's IDs are UUIDs and a template's is a slug."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"zonic-fixture:{scenario.fixture}"))


def names(scenario: Scenario) -> dict[str, str]:
    """Node names by ID, of the workflow and of its published children."""
    found: dict[str, str] = {}
    for path in [fixture_path(scenario), *(BUILDER / f"{c}.json" for c in scenario.children)]:
        found |= {n.id: n.name or n.id for n in load_document(path).nodes}
    return found


def completion_order(path: Sequence[str]) -> list[str]:
    """A start-order path in finish order: a sub-flow node finishes after its child's visits."""
    order: list[str] = []
    for index, name in enumerate(path):
        nested = [i for i, later in enumerate(path) if i > index and later.startswith(f"{name}/")]
        if not nested:
            order.append(name)
    for index, name in enumerate(path):
        nested = [i for i, later in enumerate(path) if i > index and later.startswith(f"{name}/")]
        if nested:
            order.insert(order.index(path[nested[-1]]) + 1, name)
    return order


def visit_name(by_id: dict[str, str], node_id: str, path: str | None) -> str:
    """A visit as the scenario writes it: its name, under its sub-flow nodes' names when nested."""
    return "/".join([*(by_id.get(p, p) for p in (path or "").split("/") if p), by_id[node_id]])


def normalized(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: normalized(v) for k, v in value.items() if k not in VOLATILE}
    if isinstance(value, list):
        return [normalized(v) for v in value]
    if isinstance(value, str):
        return UUID.sub("<uuid>", value)
    return value


def golden(name: str, found: Any) -> None:
    target = GOLDEN / f"{name}.json"
    if os.environ.get("ZONIC_UPDATE_SNAPSHOTS") == "1":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(found, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    assert target.is_file(), f"no golden stream for {name}; run with ZONIC_UPDATE_SNAPSHOTS=1"
    assert found == json.loads(target.read_text(encoding="utf-8"))


async def run_turns(scenario: Scenario, mode: str) -> list[dict[str, Any]]:
    """Each turn's path (node names), result and, in stream mode, events."""
    raw = read_json(fixture_path(scenario))
    wid = workflow_id(scenario)
    if scenario.responder is not None:
        script: list[Any] = [scenario.responder()] * RESPONSES
    else:
        script = [turn for t in scenario.turns for turn in t.script]
    recordings = Recordings.of(*scenario.recordings) if scenario.recordings else None
    platform = scenario.server_tools is not None
    ports = None
    if scenario.mcp or recordings or platform or scenario.sandbox:
        fake = FakeMcpServer(
            answers=dict(scenario.mcp),
            recordings=recordings,
            fallback=ok(None, {"ok": True}) if platform else None,
        )
        service: Any = McpService(fake.pool())
        ports = ServicePorts(
            mcp=ServerToolsMcp(service, scenario.server_tools or {}) if platform else service,
            egress=FakeEgress(RESOURCES.egress, recordings=recordings) if recordings else None,
            sandbox=FakePiston().sandbox() if scenario.sandbox else None,
        )
    workflows = {wid: raw}
    for child in scenario.children:  # under their own IDs: the sub-flow nodes name them
        found = read_json(BUILDER / f"{child}.json")
        workflows[(found.get("document") or found)["agenticWorkflowId"]] = found
    app = make_app(script, resources=RESOURCES, workflows=workflows, ports=ports)
    by_id = names(scenario)
    found: list[dict[str, Any]] = []
    session: str | None = None
    async with app.client() as client:
        for turn in scenario.turns:
            body: dict[str, Any] = {"agenticWorkflowId": wid, "input": {"text": turn.text}, "mode": mode}
            if session is not None:
                body["sessionId"] = session
            response = await client.post("/v1/runs", json=body, headers=auth())
            assert response.status_code == 200, response.text
            if mode == "stream":
                events = sse_events(response.text)
                run_id = response.headers["x-run-id"]
                for answer in turn.answers:  # each resumes the run where it waits
                    assert events[-1]["type"] == "run.waiting", events[-1]
                    pending = events[-1]["data"]["interruptIds"][0]
                    resumed = await client.post(
                        f"/v1/runs/{run_id}/interrupts/{pending}",
                        json={"answer": answer, "mode": mode},
                        headers=auth(),
                    )
                    assert resumed.status_code == 200, resumed.text
                    events += sse_events(resumed.text)
                assert events[-1]["type"] == "run.completed", events[-1]
                session = response.headers["x-session-id"]
                path = [
                    visit_name(by_id, e["nodeId"], e.get("path"))
                    for e in events
                    if e["type"] == "node.started"
                ]
                found.append({"path": path, "result": events[-1]["data"].get("result"), "events": events})
            else:
                answer = response.json()
                for reply in turn.answers:
                    assert answer["status"] == "waiting_input", answer
                    run_id = answer["runId"]
                    listed = await client.get(f"/v1/runs/{run_id}/interrupts", headers=auth())
                    pending = listed.json()["items"][0]["interruptId"]
                    resumed = await client.post(
                        f"/v1/runs/{run_id}/interrupts/{pending}",
                        json={"answer": reply, "mode": mode},
                        headers=auth(),
                    )
                    assert resumed.status_code == 200, resumed.text
                    answer = resumed.json()
                assert answer["status"] == "succeeded", answer
                session = answer["sessionId"]
                nodes = await client.get(f"/v1/runs/{answer['runId']}/nodes", headers=auth())
                path = [visit_name(by_id, e["nodeId"], e.get("path")) for e in nodes.json()["items"]]
                found.append({"path": path, "result": answer.get("result")})
    return found


# --- F-07 --------------------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", RUNNABLE, ids=[s.name for s in RUNNABLE])
async def test_F07_a_builder_scenario_takes_its_path_in_stream_and_sync_mode(scenario: Scenario) -> None:
    streamed = await run_turns(scenario, "stream")
    synced = await run_turns(scenario, "sync")
    for turn, stream, sync in zip(scenario.turns, streamed, synced, strict=True):
        assert stream["path"] == list(turn.path)  # the order visits start
        assert sync["path"] == completion_order(turn.path)  # the timeline: the order they finish
        # Equal but for generated IDs (a dataset reference in the answer carries a new dataset ID).
        assert normalized(stream["result"]) == normalized(sync["result"]) and stream["result"]
    golden(scenario.name, [normalized(t["events"]) for t in streamed])


ROUTED = [s for s in RUNNABLE if "F-08" in s.notes]


@pytest.mark.parametrize("scenario", ROUTED, ids=[s.name for s in ROUTED])
async def test_F08_routing_and_joins_take_the_scripted_branch_and_each_join_runs_once(
    scenario: Scenario,
) -> None:
    """The fsh fixtures route by handoff, by rule (``flow.analysisType``, ``flow.useDBQuery``) and
    by joins: each node of the path starts once, and nodes off the path never start."""
    names_by_id = names(scenario)
    for turn, found in zip(scenario.turns, await run_turns(scenario, "stream"), strict=True):
        assert found["path"] == list(turn.path)
        assert len(set(found["path"])) == len(found["path"])  # a join with several incoming runs once
        off_path = set(names_by_id.values()) - set(turn.path)
        started = {
            visit_name(names_by_id, e["nodeId"], e.get("path"))
            for e in found["events"]
            if e["type"] == "node.started"
        }
        assert not started & off_path


@pytest.mark.parametrize(
    "scenario", [s for s in SCENARIOS if s.unreachable], ids=[s.name for s in SCENARIOS if s.unreachable]
)
async def test_F07_unreachable_nodes_are_reported_and_never_compiled_or_scheduled(scenario: Scenario) -> None:
    raw = read_json(fixture_path(scenario))
    blueprint = PLANNER.build(LOADER.from_stored(raw, scenario.name))
    by_name = {name: node_id for node_id, name in names(scenario).items()}
    unreachable = {by_name[n] for n in scenario.unreachable}
    reported = {w.node_id for w in blueprint.warnings if w.code == "NODE_UNREACHABLE"}
    assert unreachable <= reported
    assert all(not blueprint.nodes[n].compiled for n in unreachable)
    [first] = await run_turns(replace(scenario, turns=scenario.turns[:1]), "stream")
    assert not {e.get("nodeId") for e in first["events"]} & unreachable


async def test_a_workflow_that_cannot_be_planned_is_refused_before_any_run_exists() -> None:
    for scenario in (s for s in SCENARIOS if s.refused):
        raw = read_json(fixture_path(scenario))
        wid = workflow_id(scenario)
        app = make_app(resources=RESOURCES, workflows={wid: raw})
        async with app.client() as client:
            response = await client.post(
                "/v1/runs",
                json={"agenticWorkflowId": wid, "input": {"text": "hi"}, "mode": "sync"},
                headers=auth(),
            )
        assert response.status_code == 422 and response.json()["error"]["code"] == scenario.refused
        assert app.gateway.count("procedure") == 0 and app.gateway.count("batch") == 0  # no run row


# --- coverage (Section 20.4) ---------------------------------------------------------------------


def vendored() -> list[Path]:
    return [p for p in document_files() if p.is_relative_to(BUILDER)]


def _workflow_ids() -> dict[str, list[Path]]:
    found: dict[str, list[Path]] = {}
    for path in vendored():
        raw = read_json(path)
        workflow = (raw.get("document") or raw).get("agenticWorkflowId")
        if isinstance(workflow, str):
            found.setdefault(workflow, []).append(path)
    return found


def _plan(path: Path) -> Any:
    try:
        blueprint = PLANNER.build(LOADER.from_stored(read_json(path), fixture_id(path)))
    except ZonicError:
        return None
    return None if blueprint.errors else blueprint


def kinds_of(path: Path, seen: frozenset[Path] = frozenset()) -> set[str] | None:
    """The kinds of the nodes a document compiles, and of its sub-flows' children (followed in
    the same dialect folder first); ``None`` when it cannot be planned. A child no fixture holds
    is ``missing-child``; configured agent and LLM tools are ``tools``."""
    blueprint = _plan(path)
    if blueprint is None:
        return None
    compiled = [s for s in blueprint.nodes.values() if s.compiled]
    kinds = {s.kind for s in compiled}
    if any(getattr(s.config, "tools", None) for s in compiled):
        kinds.add("tools")  # configured agent and LLM tools wait for the tool factory
    ids = _workflow_ids()
    for child in blueprint.children:
        candidates = sorted(ids.get(child, []), key=lambda p: p.parent != path.parent)
        if not candidates:
            kinds.add("missing-child")
            continue
        if candidates[0] in seen or candidates[0] == path:
            continue
        found = kinds_of(candidates[0], seen | {path})
        kinds |= found if found is not None else {"missing-child"}
    return kinds


def test_every_vendored_workflow_has_a_scenario_or_waits_for_the_task_building_its_kinds() -> None:
    done = set(yaml.safe_load(PROGRESS.read_text(encoding="utf-8"))["completed_tasks"] or [])
    built = set(core_kinds())
    # A requirement that is not a node kind (configured tools) is built once its task is complete.
    built |= {k for k, task in WAITING.items() if k not in KIND_NAMES and task in done}
    covered = {s.fixture for s in SCENARIOS}
    waiting: dict[str, list[str]] = {}
    problems = []
    for path in vendored():
        name = path.relative_to(BUILDER).with_suffix("").as_posix()
        kinds = kinds_of(path)
        if kinds is None:
            if not any(s.fixture == name and s.refused for s in SCENARIOS):
                problems.append(f"{name}: cannot be planned and has no refused scenario")
            continue
        stale = sorted(w for s in SCENARIOS if s.fixture == name for w in s.waits if WAITING.get(w) in done)
        if stale:
            problems.append(f"{name}: its scenario still waits for {stale}, built by now; cover that path")
        if name in FIXTURE_GAPS:
            kinds.discard("missing-child")
        missing = sorted(kinds - built)
        unknown = [k for k in missing if k not in WAITING]
        late = [f"{k} ({WAITING[k]})" for k in missing if k in WAITING and WAITING[k] in done]
        if unknown:
            problems.append(f"{name}: no task builds {unknown}")
        elif late:
            problems.append(f"{name}: {late} complete, scenario still missing")
        elif missing:
            waiting[name] = missing
            accepted = {w for s in SCENARIOS if s.fixture == name for w in s.waits}
            if name in covered and set(missing) - accepted:
                problems.append(f"{name}: has a scenario but needs {sorted(set(missing) - accepted)}")
        elif name not in covered:
            problems.append(f"{name}: runs with today's kinds but has no scenario")
    assert problems == [], "\n".join(problems)
    assert not waiting, waiting  # P4-02 built the last node kind: no vendored workflow waits


def test_every_node_type_appears_in_a_vendored_or_synthetic_fixture() -> None:
    seen = {
        node.type
        for path in [*vendored(), *sorted(SYNTHETIC.glob("*.json"))]
        for node in load_document(path).nodes
    }
    assert set(RESOURCES.manifest.node_types) <= seen, sorted(set(RESOURCES.manifest.node_types) - seen)


def test_every_golden_stream_belongs_to_a_scenario() -> None:
    assert {p.stem for p in GOLDEN.glob("*.json")} == {s.name for s in RUNNABLE}
