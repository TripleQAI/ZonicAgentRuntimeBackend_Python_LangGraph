"""Interrupts and resume (P4-02: N-20, M-10, H-01 to H-05, H-07 to H-11, L-09, F-13), through the
app on the fake gateway and, for the answer models, the harness.

Approval Flow (Start_1 -> LLM_1 -> Approval_1 -> Approved / Rejected) for most cases; small
documents built here for Human Input, a sub-flow, two parallel approvals and a hook.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import replace
from typing import Any

import pytest
from langgraph.types import Command
from pydantic import ValidationError

from tests.api.app import DEV, FakeApp, auth, make_app, sse_events
from tests.fakes.scripted_chat_model import Turn
from tests.kinds.harness import Harness, with_checkpointer
from tests.scenarios.builder.scenarios import SCENARIOS
from tests.scenarios.builder.test_builder_scenarios import run_turns
from tests.workflow_fixtures import BUILDER, SYNTHETIC, read_json
from zonic_runtime.engine.kinds.core import core_kinds
from zonic_runtime.errors import ZonicError
from zonic_runtime.integrations.langfuse.provider import trace_id

FLOW, PARENT, CHILD = "wf-approval", str(uuid.uuid4()), str(uuid.uuid4())
APPROVAL = read_json(BUILDER / "templates" / "approval_flow.json")
DRAFT = "Dear customer, your refund is on its way."


def start(workflow: str = FLOW, mode: str = "stream", text: str = "refund order 1042") -> dict[str, Any]:
    return {"agenticWorkflowId": workflow, "input": {"text": text}, "mode": mode}


async def paused(app: FakeApp, client: Any, workflow: str = FLOW) -> tuple[str, str, list[dict[str, Any]]]:
    """Starts a stream run that pauses; its run ID, first pending interrupt ID and events."""
    response = await client.post("/v1/runs", json=start(workflow), headers=auth())
    events = sse_events(response.text)
    assert events[-1]["type"] == "run.waiting", events[-1]
    return response.headers["x-run-id"], events[-1]["data"]["interruptIds"][0], events


async def answer(
    client: Any, run_id: str, interrupt_id: str, value: Any, mode: str = "sync", **headers: str
) -> Any:
    body = {"answer": value, "mode": mode}
    return await client.post(
        f"/v1/runs/{run_id}/interrupts/{interrupt_id}", json=body, headers=headers or auth()
    )


def started(events: list[dict[str, Any]]) -> list[tuple[str, int, bool]]:
    return [
        (e["nodeId"], e["data"]["visit"], e["data"]["resumed"]) for e in events if e["type"] == "node.started"
    ]


def document(nodes: list[dict[str, Any]], edges: list[tuple[str, ...]], **extra: Any) -> dict[str, Any]:
    links = [
        {"source": e[0], "target": e[1], **({"sourceHandle": e[2]} if len(e) > 2 else {})} for e in edges
    ]
    head = {"id": "start", "type": "start", "name": "Start", "config": {}}
    return {"schemaVersion": "2.0", "nodes": [head, *nodes], "edges": links, **extra}


def output(**mapping: str) -> dict[str, Any]:
    entries = [{"target": k, "source": v, "type": "string"} for k, v in mapping.items()]
    return {"id": "out", "type": "output", "name": "Out", "config": {"outputMapping": entries}}


# --- N-20, M-10 ----------------------------------------------------------------------------------


QUESTIONS = document(
    [
        {
            "id": "ok",
            "type": "approval",
            "name": "ok",
            "config": {"message": "Publish {{system.userQuery}}?"},
        },
        {
            "id": "ask",
            "type": "human_input",
            "name": "ask",
            "config": {"prompt": "Why?", "responseVariable": "flow.why"},
        },
        output(why="{{flow.why}}", comment="{{nodes.ok.output.comment}}"),
        {
            "id": "yes",
            "type": "output",
            "name": "yes",
            "config": {"outputMapping": [{"target": "answer", "source": "published"}]},
        },
    ],
    [("start", "ok"), ("ok", "yes", "approved"), ("ok", "ask", "rejected"), ("ask", "out")],
)


async def test_N20_human_input_and_approval_wait_resume_and_route() -> None:
    app = make_app(workflows={FLOW: QUESTIONS})
    async with app.client() as client:
        run_id, approval, _ = await paused(app, client)
        rejected = await answer(
            client, run_id, approval, {"approved": False, "comment": "too vague"}, mode="stream"
        )
        events = sse_events(rejected.text)
        # Human Input pauses before its node.started: it starts once, when answered (H-11).
        assert events[-1]["type"] == "run.waiting" and [n for n, *_ in started(events)] == ["ok"]
        [ask] = (await client.get(f"/v1/runs/{run_id}/interrupts", headers=auth())).json()["items"]
        assert ask["kind"] == "input" and ask["prompt"] == {"text": "Why?"}
        done = (await answer(client, run_id, ask["interruptId"], {"value": "name the meters"})).json()
    assert done["status"] == "succeeded"
    assert done["result"] == {"why": "name the meters", "comment": "too vague"}  # responseVariable flow.why


def test_M10_answer_models_follow_the_node_configuration() -> None:
    from types import SimpleNamespace

    from zonic_runtime.engine.kinds.interrupt_kind import InterruptKind
    from zonic_runtime.engine.node_template import NodeServices

    services = NodeServices.from_resources(DEV)
    blueprint = Harness(DEV, core_kinds()).plan(
        document(
            [
                {"id": "n", "type": "human_input", "config": {"inputType": "number", "required": False}},
                {"id": "t", "type": "human_input", "config": {}},
                {"id": "a", "type": "approval", "config": {}},
            ],
            [("start", "n"), ("n", "t"), ("t", "a")],
        )
    )

    def schema(node_id: str) -> dict[str, Any]:
        spec = blueprint.nodes[node_id]
        ctx: Any = SimpleNamespace(spec=spec, services=services)
        return InterruptKind(spec, blueprint).answer_model(ctx).model_json_schema(by_alias=True)

    number, text, decision = schema("n"), schema("t"), schema("a")
    assert number["properties"]["value"]["anyOf"][0] == {"type": "number"} and "required" not in number
    assert text["properties"]["value"]["type"] == "string" and text["required"] == ["value"]
    assert decision["required"] == ["approved"] and set(decision["properties"]) >= {"approved", "comment"}


async def test_M10_langgraph_validates_resume_values_with_the_answer_model() -> None:
    h = with_checkpointer(Harness(DEV, core_kinds()))
    blueprint = h.plan(document([{"id": "a", "type": "approval", "config": {}}], [("start", "a")]))
    graph = h.compile(blueprint)
    config, context = h.config(blueprint), h.context(blueprint)
    [pending] = (await graph.ainvoke({}, config=config, context=context))["__interrupt__"]
    assert pending.response_schema["properties"]["approved"]["type"] == "boolean"
    with pytest.raises(ZonicError) as caught:  # the answer model rejects it (the API checks first)
        await graph.ainvoke(
            Command(resume={pending.id: {"approved": "maybe"}}), config=config, context=context
        )
    assert caught.value.code == "INTERRUPT_ANSWER_INVALID"
    assert isinstance(caught.value.__cause__, ValidationError)
    config, context = h.config(blueprint, "run-2"), h.context(blueprint, "run-2")
    [pending] = (await graph.ainvoke({}, config=config, context=context))["__interrupt__"]
    final = await graph.ainvoke(
        Command(resume={pending.id: {"approved": True}}), config=config, context=context
    )
    assert final["nodes"]["a"]["output"]["approved"] is True


# --- H-01, H-02, H-05 ---------------------------------------------------------------------------


async def test_H01_a_valid_answer_resumes_from_the_stored_revision_with_the_restored_buffer() -> None:
    app = make_app([Turn.say(DRAFT)], workflows={FLOW: APPROVAL})
    async with app.client() as client:
        run_id, interrupt_id, _ = await paused(app, client)
        assert len(app.schema.t("runtime_checkpoint")) == 1  # one checkpoint row (P-08)
        resumed = await answer(client, run_id, interrupt_id, {"approved": True}, mode="stream")
    events = sse_events(resumed.text)
    assert started(events) == [("approval_1", 1, True), ("output_approved", 1, False)]
    assert events[-1]["data"]["result"] == {"response": DRAFT}  # LLM_1's output came from the buffer
    assert len(app.models.model.requests) == 1  # LLM_1 never ran again
    assert app.schema.t("runtime_checkpoint") == []  # sp_run_finish deleted it
    [row] = [i for i in app.schema.t("runtime_interrupt") if i["interrupt_id"] == interrupt_id]
    assert row["status"] == "answered" and row["answer"] == {"approved": True, "decidedBy": "alice"}


async def test_H02_an_invalid_a_second_and_another_users_answer() -> None:
    app = make_app([Turn.say(DRAFT)], workflows={FLOW: APPROVAL})
    async with app.client() as client:
        run_id, interrupt_id, _ = await paused(app, client)
        invalid = await answer(client, run_id, interrupt_id, {"approved": "maybe"})
        foreign = await answer(client, run_id, interrupt_id, {"approved": True}, **auth("mallory"))
        first = await answer(client, run_id, interrupt_id, {"approved": True})
        second = await answer(client, run_id, interrupt_id, {"approved": False})
    assert invalid.status_code == 400 and invalid.json()["error"]["code"] == "INTERRUPT_ANSWER_INVALID"
    assert foreign.status_code == 404
    assert first.json()["status"] == "succeeded"
    assert second.status_code == 409 and second.json()["error"]["code"] == "INTERRUPT_ALREADY_ANSWERED"


async def test_H05_a_stream_resume_continues_the_runs_seq_series() -> None:
    app = make_app([Turn.say(DRAFT)], workflows={FLOW: APPROVAL})
    async with app.client() as client:
        run_id, interrupt_id, first = await paused(app, client)
        resumed = sse_events(
            (await answer(client, run_id, interrupt_id, {"approved": True}, mode="stream")).text
        )
    seqs = [e["seq"] for e in [*first, *resumed]]
    assert seqs == list(range(1, len(seqs) + 1))
    assert resumed[0]["type"] == "run.started" and resumed[-1]["type"] == "run.completed"


# --- H-03 ---------------------------------------------------------------------------------------


def _expire(app: FakeApp) -> None:
    for row in app.schema.t("runtime_interrupt"):
        row["expires_at"] = "2020-01-01T00:00:00+00:00"


async def test_H03_an_expired_interrupt_cancels_its_run() -> None:
    app = make_app([Turn.say(DRAFT)], workflows={FLOW: APPROVAL})
    async with app.client() as client:
        run_id, interrupt_id, _ = await paused(app, client)
        _expire(app)
        assert await app.deps.expiry.run() == 1
        run = (await client.get(f"/v1/runs/{run_id}", headers=auth())).json()
        late = await answer(client, run_id, interrupt_id, {"approved": True})
    assert run["status"] == "cancelled" and run["pendingInterrupts"] == []
    assert late.status_code in (404, 410)


async def test_H03_with_on_expiry_reject_an_expired_approval_takes_rejected() -> None:
    app = make_app([Turn.say(DRAFT)], workflows={FLOW: APPROVAL}, interrupts={"on_expiry": "reject"})
    async with app.client() as client:
        run_id, _, _ = await paused(app, client)
        _expire(app)
        assert await app.deps.expiry.run() == 1
        outcome = await app.deps.registry.wait(run_id, 5)
        run = (await client.get(f"/v1/runs/{run_id}", headers=auth())).json()
    assert outcome is not None and outcome.status == "succeeded"
    assert run["status"] == "succeeded" and run["result"] == {"response": "The draft was rejected."}


# --- H-04 ---------------------------------------------------------------------------------------


async def test_H04_a_run_paused_on_one_instance_resumes_on_another() -> None:
    first = make_app([Turn.say(DRAFT)], workflows={FLOW: APPROVAL})
    async with first.client() as client:
        run_id, interrupt_id, _ = await paused(first, client)
    second = first.twin()  # a new process: no buffer, no caches, no stream
    assert second.deps.runtime.saver is not first.deps.runtime.saver
    async with second.client() as client:
        done = (await answer(client, run_id, interrupt_id, {"approved": True})).json()
    assert done["status"] == "succeeded" and done["result"] == {"response": DRAFT}
    assert second.models.model.requests == []  # nothing ran twice


# --- H-07, H-08, H-09, H-10, H-11 ----------------------------------------------------------------


def _child() -> dict[str, Any]:
    llm = {
        "id": "llm",
        "type": "llm",
        "name": "llm",
        "config": {"messages": [{"role": "user", "content": "Draft."}]},
    }
    ask = {
        "id": "ask",
        "type": "human_input",
        "name": "ask",
        "config": {"prompt": "Tone?", "responseVariable": "flow.tone"},
    }
    out = output(answer="{{nodes.llm.output.text}} ({{flow.tone}})")
    return document(
        [llm, ask, out], [("start", "llm"), ("llm", "ask"), ("ask", "out")], agenticWorkflowId=CHILD
    )


async def test_H07_human_input_inside_a_sub_flow_pauses_the_run_and_resumes_the_child() -> None:
    from tests.fakes.subflows import subflow

    parent = document(
        [subflow("sub", CHILD, inputs=[]), output(answer="{{nodes.sub.output.result.answer}}")],
        [("start", "sub"), ("sub", "out")],
        agenticWorkflowId=PARENT,
    )
    app = make_app([Turn.say("A draft")], workflows={PARENT: parent, CHILD: _child()})
    async with app.client() as client:
        run_id, interrupt_id, _ = await paused(app, client, PARENT)
        resumed = sse_events(
            (await answer(client, run_id, interrupt_id, {"value": "warm"}, mode="stream")).text
        )
    nested = [(e["nodeId"], e.get("path")) for e in resumed if e["type"] == "node.started"]
    assert nested == [("sub", None), ("ask", "sub"), ("out", "sub"), ("out", None)]  # llm not again
    assert len(app.models.model.requests) == 1
    assert resumed[-1]["data"]["result"] == {"answer": "A draft (warm)"}


async def test_H08_an_approval_inside_a_loop_resumes_the_same_visit() -> None:
    loop = read_json(SYNTHETIC / "approval-in-loop.json")
    app = make_app([Turn.say("Draft one"), Turn.say("Draft two")], workflows={FLOW: loop})
    async with app.client() as client:
        run_id, first_id, _ = await paused(app, client)
        again = sse_events((await answer(client, run_id, first_id, {"approved": False}, mode="stream")).text)
        assert started(again) == [("approval_1", 1, True), ("draft_1", 2, False)]
        second_id = again[-1]["data"]["interruptIds"][0]
        done = sse_events((await answer(client, run_id, second_id, {"approved": True}, mode="stream")).text)
    assert started(done) == [("approval_1", 2, True), ("output_1", 1, False)]
    assert done[-1]["data"]["result"] == {"answer": "Draft two"}


async def test_H09_a_document_edited_while_waiting_resumes_on_the_stored_one() -> None:
    app = make_app([Turn.say(DRAFT), Turn.say("Another draft.")], workflows={FLOW: APPROVAL})
    edited = copy.deepcopy(APPROVAL)
    for node in edited["document"]["nodes"]:
        if node["id"] == "output_approved":
            node["config"]["outputMapping"][0]["source"] = "edited"
    async with app.client() as client:
        run_id, interrupt_id, _ = await paused(app, client)
        for table in ("v_agent_export", "v_agent_summary"):
            app.gateway.tables[("builder", table)] = []
        app.gateway.tables[("runtime", "v_agent_catalog")] = []
        app.publish(FLOW, edited)
        for key in (("runtime", "v_agent_catalog"), ("builder", "v_agent_summary")):
            for row in app.gateway.tables[key]:
                row["updated_at"] = "2026-12-01T00:00:00+00:00"  # the builder saved a new revision
        app.deps.runtime.loader._catalog = None  # past catalog.cacheSeconds
        old = (await answer(client, run_id, interrupt_id, {"approved": True})).json()
        run_id, interrupt_id, _ = await paused(app, client)
        new = (await answer(client, run_id, interrupt_id, {"approved": True})).json()
    assert old["result"] == {"response": DRAFT} and new["result"] == {"response": "edited"}


PARALLEL = document(
    [
        {"id": "a", "type": "approval", "name": "a", "config": {"message": "A?"}},
        {"id": "b", "type": "approval", "name": "b", "config": {"message": "B?"}},
        output(a="{{nodes.a.output.approved}}", b="{{nodes.b.output.approved}}"),
    ],
    [("start", "a"), ("start", "b"), ("a", "out", "approved"), ("b", "out", "approved")],
)


async def test_H10_two_parallel_interrupts_are_both_pending_and_answered_by_id() -> None:
    app = make_app(workflows={FLOW: PARALLEL})
    async with app.client() as client:
        response = await client.post("/v1/runs", json=start(), headers=auth())
        run_id = response.headers["x-run-id"]
        listed = (await client.get(f"/v1/runs/{run_id}/interrupts", headers=auth())).json()["items"]
        assert sorted(i["nodeId"] for i in listed) == ["a", "b"]
        by_node = {i["nodeId"]: i["interruptId"] for i in listed}
        partial = await answer(client, run_id, by_node["b"], {"approved": True})
        assert partial.json()["status"] == "waiting_input"  # a's branch still waits (SP-8)
        [left] = (await client.get(f"/v1/runs/{run_id}/interrupts", headers=auth())).json()["items"]
        assert left["nodeId"] == "a" and left["interruptId"] != by_node["a"]  # this pause's row
        stale = await answer(client, run_id, by_node["a"], {"approved": True})
        done = (await answer(client, run_id, left["interruptId"], {"approved": True})).json()
    assert stale.status_code == 404
    assert done["status"] == "succeeded" and done["result"] == {"a": "true", "b": "true"}


async def test_H11_nothing_before_interrupt_has_side_effects() -> None:
    from tests.unit.test_hooks import CALLS, RecordHook, binding
    from zonic_runtime.models.hooks.binding import HookBindings

    CALLS.clear()
    hooks = HookBindings.model_validate(
        {
            "bindings": [
                binding("seen", "beforeNode", match={"nodeTypes": ["approval"]}, config={"label": "x"})
            ]
        }
    )
    app = make_app(
        [Turn.say(DRAFT)],
        resources=replace(DEV, hooks=hooks),
        workflows={FLOW: APPROVAL},
        hook_types=[RecordHook()],
    )
    async with app.client() as client:
        run_id, interrupt_id, first = await paused(app, client)
        assert not [c for c in CALLS if c[1] == "approval_1"]  # no beforeNode on the pausing pass
        assert "approval_1" not in [n for n, *_ in started(first)]
        resumed = sse_events(
            (await answer(client, run_id, interrupt_id, {"approved": True}, mode="stream")).text
        )
    assert [c[1] for c in CALLS if c[1] == "approval_1"] == ["approval_1"]
    assert started(resumed)[0] == ("approval_1", 1, True) and len(app.models.model.requests) == 1


# --- L-09 ---------------------------------------------------------------------------------------


async def test_L09_the_trace_is_the_same_across_the_wait_with_requested_and_answered_events() -> None:
    export = DEV.settings.observability.export.model_copy(update={"retry_backoff_milliseconds": 5})
    app = make_app(
        [Turn.say(DRAFT)], workflows={FLOW: APPROVAL}, tracing=True, observability={"export": export}
    )
    assert app.deps.tracing is not None and app.obs is not None
    app.deps.tracing.start()
    try:
        async with app.client() as client:
            run_id, interrupt_id, _ = await paused(app, client)
            await answer(client, run_id, interrupt_id, {"approved": True})
        await app.deps.tracing.flush()
    finally:
        await app.deps.tracing.aclose()
    found = list(app.obs.observations.values())
    assert {o["traceId"] for o in found} == {trace_id(run_id)}
    names = [o.get("name") for o in found]
    assert "interrupt.requested" in names and "interrupt.answered" in names
    nodes = {o.get("nodeId") for o in found if o.get("visit")}
    assert {"llm_1", "approval_1", "output_approved"} <= nodes  # the resumed nodes join the trace


# --- F-13 ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["approval-flow", "facility-operations-swarm"])
async def test_F13_approvals_pause_and_resume_across_requests_into_their_branches(name: str) -> None:
    scenario = next(s for s in SCENARIOS if s.name == name)
    for turn, found in zip(scenario.turns, await run_turns(scenario, "sync"), strict=True):
        if turn.answers:
            assert any(n in found["path"] for n in ("Approval_1", "publish_approval"))
        assert found["result"]
