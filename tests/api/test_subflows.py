"""Sub-flows through the app (P3-09: N-11, G-05, S-04 and L-06 sub-flow parts, F-10).

Parent -> child -> grandchild documents from ``tests/fakes/subflows.py``; F-10 runs the vendored
``all-agents`` supervisor into its ``fsh-facility-agent-v2`` child (the children that need tools,
HTTP and database nodes are published but not entered).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from tests.api.app import DEV, auth, make_app, sse_events
from tests.fakes.scripted_chat_model import Turn
from tests.fakes.subflows import CHILD, GRANDCHILD, PARENT, chain, child, grandchild, parent, subflow
from tests.workflow_fixtures import BUILDER, read_json


def start(workflow: str = PARENT, mode: str = "sync", text: str = "status?") -> dict[str, Any]:
    return {"agenticWorkflowId": workflow, "input": {"text": text}, "mode": mode}


async def run(
    workflows: dict[str, Any], script: list[Any], mode: str = "sync", **settings: Any
) -> tuple[Any, Any]:
    app = make_app(script, workflows=workflows, **settings)
    async with app.client() as client:
        response = await client.post("/v1/runs", json=start(mode=mode), headers=auth())
    return app, response


# --- N-11 --------------------------------------------------------------------------------------


async def test_N11_inputs_reach_the_child_and_its_result_is_the_nodes_output() -> None:
    app, response = await run(chain(), [Turn.say("Boilers are fine.")])
    body = response.json()
    assert body["status"] == "succeeded" and body["result"] == {"answer": "Boilers are fine."}
    [request] = app.models.model.requests
    assert (
        request.messages[-1].content == "Topic boilers: status?"
    )  # topic mapped parent -> child -> grandchild
    assert body["usage"]["modelCalls"] == 1  # the child's usage is the node's


@pytest.mark.parametrize(
    ("mapping", "answer"),
    [
        ({"answer": "{{nodes.llm.output.text}}"}, "Boilers are fine."),
        (
            {"answer": "{{nodes.llm.output.text}}", "level": "low"},
            {"answer": "Boilers are fine.", "level": "low"},
        ),
    ],
)
async def test_N11_a_parent_without_an_output_mapping_answers_with_the_childs_mapped_result(
    mapping: dict[str, str], answer: str | dict[str, str]
) -> None:
    # The child's answer is what its own chat would show: its only string field as text, else its
    # whole result. Its LLM's messages are not chat answers, so they cannot stand in for it.
    llm = {
        "id": "llm",
        "type": "llm",
        "name": "llm",
        "config": {"messages": [{"role": "user", "content": "{{system.userQuery}}"}]},
    }
    out = {
        "id": "out",
        "type": "output",
        "name": "Out",
        "config": {"outputMapping": [{"target": k, "value": v} for k, v in mapping.items()]},
    }
    workflows = {
        PARENT: {
            "schemaVersion": "2.0",
            "agenticWorkflowId": PARENT,
            "nodes": [
                {"id": "start", "type": "start", "name": "Start", "config": {}},
                subflow("child", CHILD),
                {"id": "out", "type": "output", "name": "Out", "config": {"outputMapping": []}},
            ],
            "edges": [{"source": "start", "target": "child"}, {"source": "child", "target": "out"}],
        },
        CHILD: {
            "schemaVersion": "2.0",
            "agenticWorkflowId": CHILD,
            "nodes": [{"id": "start", "type": "start", "name": "Start", "config": {}}, llm, out],
            "edges": [{"source": "start", "target": "llm"}, {"source": "llm", "target": "out"}],
        },
    }
    _, response = await run(workflows, [Turn.say("Boilers are fine.")])
    body = response.json()
    assert body["status"] == "succeeded", body
    assert body["result"] == (answer if isinstance(answer, dict) else {"text": answer})


@pytest.mark.parametrize(("policy", "seen"), [("declared", ""), ("all", "s3cret"), ("none", "")])
async def test_N11_the_child_sees_flow_per_inherit_flow(policy: str, seen: str) -> None:
    workflows = {
        PARENT: parent(secret="{{nodes.child.output.secret}}", topic="{{nodes.child.output.topic}}"),
        CHILD: child(secret="{{flow.secret}}", topic="{{flow.topic}}"),
        GRANDCHILD: grandchild(),
    }
    _, response = await run(workflows, [Turn.say("ok")], subflow={"inherit_flow": policy})
    result = response.json()["result"]
    assert (result.get("secret") or "") == seen  # the child declares topic only
    assert (result.get("topic") or "") == ("" if policy == "none" else "boilers")


async def test_N11_a_chain_deeper_than_max_depth_is_subflow_depth_exceeded() -> None:
    _, response = await run(chain(), [Turn.say("never")], subflow={"max_depth": 1})
    body = response.json()
    assert body["status"] == "failed" and body["error"]["code"] == "SUBFLOW_DEPTH_EXCEEDED"


async def test_N11_a_workflow_already_on_the_chain_is_subflow_cycle() -> None:
    looping = child()
    looping["nodes"][1] = subflow("grand", PARENT)  # the child calls its parent
    _, response = await run({PARENT: parent(), CHILD: looping}, [])
    assert response.json()["error"]["code"] == "SUBFLOW_CYCLE"


async def test_N11_a_child_that_does_not_exist_for_the_caller_is_workflow_not_found() -> None:
    missing = parent()
    missing["nodes"][2] = subflow("child", str(uuid.uuid4()))
    _, response = await run({PARENT: missing}, [])
    assert response.json()["error"]["code"] == "WORKFLOW_NOT_FOUND"


async def test_N11_the_childs_visits_join_the_parents_timeline_with_their_path() -> None:
    app, response = await run(chain(), [Turn.say("ok")])
    async with app.client() as client:
        items = (await client.get(f"/v1/runs/{response.json()['runId']}/nodes", headers=auth())).json()[
            "items"
        ]
    assert [(i["nodeId"], i.get("path")) for i in items] == [
        ("start", None),
        ("set", None),
        ("start", "child"),
        ("start", "child/grand"),
        ("llm", "child/grand"),
        ("out", "child/grand"),
        ("grand", "child"),
        ("out", "child"),
        ("child", None),
        ("out", None),
    ]


# --- G-05 --------------------------------------------------------------------------------------


async def test_G05_child_graphs_come_from_the_cache_in_the_child_role_without_a_checkpointer() -> None:
    app = make_app([Turn.say("a"), Turn.say("b")], workflows=chain())
    graphs = app.deps.runtime.graphs
    async with app.client() as client:
        await client.post("/v1/runs", json=start(), headers=auth())
        compiled = graphs.compilations
        await client.post("/v1/runs", json=start(), headers=auth())
    assert compiled == 3 and graphs.compilations == 3  # root + 2 children, then all cached
    roles = {(k.workflow_id, k.role) for k in graphs._entries._items}  # type: ignore[attr-defined]
    assert {(CHILD, "child"), (GRANDCHILD, "child"), (PARENT, "root")} <= roles
    child_graph = next(v for k, v in graphs._entries._items.items() if k.role == "child")  # type: ignore[attr-defined]
    assert child_graph.checkpointer is None


# --- S-04 --------------------------------------------------------------------------------------


async def test_S04_tokens_of_a_model_inside_a_sub_flow_carry_the_builder_node_id_and_path() -> None:
    _, response = await run(chain(), [Turn.say("Boilers are fine.")], mode="stream")
    events = sse_events(response.text)
    tokens = [e for e in events if e["type"] == "token.delta"]
    assert tokens and all(e["nodeId"] == "llm" and e["path"] == "child/grand" for e in tokens)
    started = {(e["nodeId"], e.get("path")) for e in events if e["type"] == "node.started"}
    assert {("grand", "child"), ("llm", "child/grand"), ("child", None)} <= started


# --- L-06 (sub-flow part) ------------------------------------------------------------------------


async def test_L06_the_childs_node_observations_nest_under_the_sub_flow_node() -> None:
    export = DEV.settings.observability.export.model_copy(update={"retry_backoff_milliseconds": 5})
    app = make_app([Turn.say("ok")], workflows=chain(), tracing=True, observability={"export": export})
    assert app.deps.tracing is not None and app.obs is not None
    app.deps.tracing.start()
    try:
        async with app.client() as client:
            await client.post("/v1/runs", json=start(), headers=auth())
        await app.deps.tracing.flush()
    finally:
        await app.deps.tracing.aclose()
    found = list(app.obs.observations.values())
    parents = {o["observationId"]: o.get("parentObservationId") for o in found}
    nodes = {
        (o.get("nodeId"), o.get("nodeType")): o for o in found if o.get("visit") == 1 and o.get("nodeType")
    }

    def under(observation: dict[str, Any], ancestor: dict[str, Any]) -> bool:
        current = parents.get(observation["observationId"])
        while current is not None and current != ancestor["observationId"]:
            current = parents.get(current)
        return current == ancestor["observationId"]

    sub, grand, llm = nodes[("child", "workflow")], nodes[("grand", "workflow")], nodes[("llm", "llm")]
    assert sub["type"] == "chain" and under(grand, sub) and under(llm, grand)


# --- F-10 --------------------------------------------------------------------------------------


def vendored(dialect: str = "zonic") -> dict[str, Any]:
    found = {}
    for name in ("all-agents", "fsh-energy-agent", "fsh-facility-agent-v2", "fsh-risk-agent"):
        raw = read_json(BUILDER / dialect / f"{name}.json")
        found[raw["agenticWorkflowId"]] = raw
    return found


@pytest.mark.parametrize("dialect", ["zonic", "zonic-versioned"])
async def test_F10_the_supervisor_hands_off_into_a_child_loaded_with_its_siblings_in_one_read(
    dialect: str,
) -> None:
    workflows = vendored(dialect)
    root = next(i for i, d in workflows.items() if d["name"] == "all-agents")
    script = [
        Turn.call("handoff_to", {"target": "facility-energy", "reason": "facility question"}),
        Turn.say('{"keywords": ["boiler"]}'),
        Turn.call("handoff_to", {"target": "direct-answer"}),
        Turn.say("<p>Three facilities.</p>"),
    ]
    resources = DEV.models.model_copy(
        update={"models": {c: e.model_copy(update={"enabled": True}) for c, e in DEV.models.models.items()}}
    )
    from dataclasses import replace

    app = make_app(script, resources=replace(DEV, models=resources), workflows=workflows)
    async with app.client() as client:
        app.gateway.calls.clear()
        response = await client.post(
            "/v1/runs", json=start(root, mode="stream", text="facility alarms?"), headers=auth()
        )
    events = sse_events(response.text)
    assert events[-1]["type"] == "run.completed", json.dumps(events[-1])[:500]
    reads = [c for c in app.gateway.calls if c.kind == "view" and c.name == "v_agent_summary"]
    assert len(reads) == 1  # the three children, one `in` read
    paths = {e.get("path") for e in events if e["type"] == "node.started" and e.get("path")}
    chosen = next(n["id"] for n in workflows[root]["nodes"] if n.get("name") == "facility-energy")
    assert paths == {chosen}  # only the chosen child ran, its nodes under the sub-flow node (IDs, as events)
