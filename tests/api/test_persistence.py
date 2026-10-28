"""TurnSaver and per-turn persistence (P2-04: P-05 to P-09, P-11, P-13, E-19, A-07, A-08 sync part)."""

from __future__ import annotations

import itertools
import json
import os
from typing import Any

from langgraph.checkpoint.conformance import checkpointer_test, validate
from langgraph.types import Command

from tests.api.app import CHAT, auth, make_app
from tests.fakes.scripted_chat_model import Turn
from tests.kinds.harness import DEFAULT_INPUT, AskKind, EchoKind, Harness, Recorder
from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import load_resources
from zonic_runtime.engine.persistence.codec import FORMAT, CheckpointBundle, langgraph_version
from zonic_runtime.engine.persistence.turn_saver import TurnSaver, serializer
from zonic_runtime.errors import ZonicError

DEV = load_resources(EnvironmentModel(env="dev"))


def sync(text: str, **extra: Any) -> dict[str, Any]:
    return {"agenticWorkflowId": "wf-chat", "input": {"text": text}, "mode": "sync", **extra}


def wide(nodes: int) -> dict[str, Any]:
    """Start, ``nodes`` variable nodes in a chain, Output."""
    ids = [f"v{i}" for i in range(nodes)]
    body = [
        {"id": i, "type": "variable", "config": {"updates": [{"path": f"flow.{i}", "value": True}]}}
        for i in ids
    ]
    out = {
        "id": "out",
        "type": "output",
        "config": {"outputMapping": [{"target": "count", "value": "{{flow.v0}}"}]},
    }
    chain = ["start", *ids, "out"]
    return {
        "schemaVersion": "2.0",
        "nodes": [{"id": "start", "type": "start", "config": {}}, *body, out],
        "edges": [{"source": a, "target": b} for a, b in itertools.pairwise(chain)],
    }


# --- P-05, P-06 ------------------------------------------------------------------------------


async def test_P05_a_warm_20_node_turn_makes_the_start_batch_and_sp_run_finish_only() -> None:
    app = make_app(workflows={"wf-chat": wide(18)})
    async with app.client() as client:
        first = (await client.post("/v1/runs", json=sync("first"), headers=auth())).json()
        app.gateway.calls.clear()
        second = (
            await client.post("/v1/runs", json=sync("second", sessionId=first["sessionId"]), headers=auth())
        ).json()
    assert second["status"] == "succeeded" and second["result"] == {"count": True}
    assert [(c.kind, c.name) for c in app.gateway.calls] == [
        ("batch:sequential", "v_session_state,v_message,sp_run_start"),
        ("procedure", "sp_run_finish"),
    ]
    assert app.deps.runtime.saver.threads() == []  # every buffer detached after its save


async def test_P06_durability_exit_writes_one_checkpoint_at_the_end_whatever_the_node_count() -> None:
    puts = []
    for nodes in (2, 18):
        recorder = Recorder()
        saver = TurnSaver()
        h = Harness(
            DEV,
            {"variable": lambda s, b, r=recorder: EchoKind(s, b, r)},
            recorder=recorder,
            checkpointer=saver,
        )
        blueprint = h.plan(wide(nodes))
        graph = h.compile(blueprint)
        saver.attach("run-1")
        await graph.ainvoke(
            dict(DEFAULT_INPUT), h.config(blueprint), context=h.context(blueprint), durability="exit"
        )
        puts.append(saver.puts["run-1"])
    assert puts[0] == puts[1] == 1


# --- P-07 ------------------------------------------------------------------------------------


async def test_P07_turn_saver_passes_the_checkpointer_conformance_suite() -> None:
    @checkpointer_test(name="TurnSaver")
    async def saver() -> Any:
        yield TurnSaver(strict=False)

    report = await validate(saver)
    assert report.passed_all_base(), report.to_dict()
    assert {"put", "put_writes", "get_tuple", "list", "delete_thread"} <= {
        name for name, result in report.results.items() if result.passed
    }


async def test_P07_a_thread_that_was_not_attached_is_refused() -> None:
    recorder = Recorder()
    saver = TurnSaver()
    h = Harness(
        DEV, {"variable": lambda s, b: EchoKind(s, b, recorder)}, recorder=recorder, checkpointer=saver
    )
    blueprint = h.plan(wide(1))
    graph = h.compile(blueprint)
    try:
        await graph.ainvoke(dict(DEFAULT_INPUT), h.config(blueprint), context=h.context(blueprint))
    except ZonicError as error:
        assert error.code == "CHECKPOINT_THREAD_UNKNOWN"
    else:
        raise AssertionError("an unattached thread was accepted")


# --- P-08, P-09 --------------------------------------------------------------------------------


def ask_document() -> dict[str, Any]:
    return {
        "schemaVersion": "2.0",
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {"id": "ask", "type": "human_input", "config": {"prompt": "answer?"}},
            {"id": "after", "type": "variable", "config": {}},
        ],
        "edges": [{"source": "start", "target": "ask"}, {"source": "ask", "target": "after"}],
    }


def ask_harness(saver: TurnSaver) -> tuple[Harness, Recorder]:
    recorder = Recorder()
    kinds = {
        "interrupt": lambda s, b: AskKind(s, b, recorder),
        "variable": lambda s, b: EchoKind(s, b, recorder),
    }
    return Harness(DEV, kinds, recorder=recorder, checkpointer=saver), recorder


async def test_P08_a_paused_turn_resumes_from_its_bundle_on_another_instance_with_identical_state() -> None:
    first_saver = TurnSaver()
    h, _ = ask_harness(first_saver)
    blueprint = h.plan(ask_document())
    first_saver.attach("run-1")
    paused = await h.compile(blueprint).ainvoke(
        dict(DEFAULT_INPUT), h.config(blueprint), context=h.context(blueprint)
    )
    assert "__interrupt__" in paused
    body = json.loads(
        json.dumps(first_saver.detach("run-1").model_dump(by_alias=True))
    )  # through JSON, as the row
    assert first_saver.threads() == []

    second_saver = TurnSaver()  # another instance: a new process would compile the same revision
    h2, recorder = ask_harness(second_saver)
    second_saver.attach("run-1", CheckpointBundle.read(body))
    graph = h2.compile(h2.plan(ask_document()))
    restored = await graph.aget_state(h2.config(blueprint))
    assert restored.values["nodes"]["start"] == paused["nodes"]["start"] and restored.next == ("ask",)
    final = await graph.ainvoke(Command(resume="yes"), h2.config(blueprint), context=h2.context(blueprint))
    assert final["nodes"]["ask"]["output"] == {"value": "yes"} and recorder.calls == ["ask", "after"]


async def test_P09_the_codec_round_trips_every_run_state_channel_compressed_and_encrypted() -> None:
    state = {
        **DEFAULT_INPUT,
        "flow": {
            "values": {"n": 1, "x": {"deep": ["é", "👋"]}},
            "provenance": {"n": [-1, ""]},
            "conflicts": [],
        },
        "input": {"sub": True},
        "history": {"messages": [{"role": "user", "content": "earlier"}]},
    }
    for key in (None, os.urandom(32)):
        saver = TurnSaver(serde=serializer(key))
        h, _ = ask_harness(saver)
        blueprint = h.plan(ask_document())
        saver.attach("run-1")
        paused = await h.compile(blueprint).ainvoke(state, h.config(blueprint), context=h.context(blueprint))
        bundle = saver.detach("run-1")
        assert bundle.format == FORMAT and bundle.langgraph_version == langgraph_version()
        text = json.dumps(bundle.model_dump(by_alias=True))
        assert "earlier" not in text and "👋" not in text  # compressed (and, with a key, encrypted)
        again = TurnSaver(serde=serializer(key))
        again.attach("run-1", CheckpointBundle.read(json.loads(text)))
        h2, _ = ask_harness(again)
        restored = (await h2.compile(blueprint).aget_state(h2.config(blueprint))).values
        for channel in (
            "system",
            "flow",
            "input",
            "history",
            "nodes",
            "visits",
            "timeline",
            "warnings",
            "usage",
        ):
            assert restored.get(channel) == paused.get(channel), channel
        if key is not None:
            wrong = TurnSaver(serde=serializer(os.urandom(32)))
            wrong.attach("run-1", CheckpointBundle.read(json.loads(text)))
            h3, _ = ask_harness(wrong)
            try:
                await h3.compile(blueprint).aget_state(h3.config(blueprint))
            except Exception:  # any decryption failure is the point
                pass
            else:
                raise AssertionError("an encrypted bundle was read with the wrong key")


# --- P-11 ------------------------------------------------------------------------------------


async def test_P11_with_the_gateway_down_at_turn_end_the_save_retries_until_its_deadline_then_fails() -> None:
    app = make_app(
        [Turn.say("Saved later."), Turn.say("Never saved.")], state={"save_deadline_milliseconds": 200}
    )
    async with app.client() as client:
        app.gateway.fail_next("sp_run_finish", ZonicError("DATA_UNAVAILABLE"), times=2)
        saved = await client.post("/v1/runs", json={**sync("one"), "mode": "stream"}, headers=auth())
        app.gateway.fail_next("sp_run_finish", ZonicError("DATA_UNAVAILABLE"), times=1000)
        lost = await client.post("/v1/runs", json={**sync("two"), "mode": "stream"}, headers=auth())
    from tests.api.app import sse_events

    ok, failed = sse_events(saved.text), sse_events(lost.text)
    assert ok[-1]["type"] == "run.completed"
    assert failed[-1]["type"] == "run.failed" and failed[-1]["data"]["code"] == "STATE_UNAVAILABLE"
    assert "run.completed" not in [e["type"] for e in failed]


# --- P-13, E-19 ------------------------------------------------------------------------------


MEMORY: dict[str, Any] = {
    "schemaVersion": "2.0",
    "variables": {"count": {"type": "number", "default": 0}, "scratch": {"type": "string", "persist": False}},
    "nodes": [
        {"id": "start", "type": "start", "config": {}},
        {
            "id": "v",
            "type": "variable",
            "config": {
                "updates": [
                    {"path": "flow.count", "op": "increment", "value": 1},
                    {"path": "flow.scratch", "value": "{{system.userQuery}}"},
                    {
                        "path": "thread.notes",
                        "op": "append",
                        "value": "note {{system.userQuery}}",
                        "role": "user",
                    },
                ]
            },
        },
        {
            "id": "out",
            "type": "output",
            "config": {
                "outputMapping": [
                    {"target": "count", "value": "{{flow.count}}", "type": "number"},
                    {"target": "notes", "value": "{{thread.notes}}"},
                ]
            },
        },
    ],
    "edges": [{"source": "start", "target": "v"}, {"source": "v", "target": "out"}],
}


async def test_P13_declared_variables_persist_unless_persist_false_and_threads_are_read_back() -> None:
    app = make_app(workflows={"wf-chat": MEMORY})
    async with app.client() as client:
        first = (await client.post("/v1/runs", json=sync("a"), headers=auth())).json()
        second = (
            await client.post("/v1/runs", json=sync("b", sessionId=first["sessionId"]), headers=auth())
        ).json()
    assert first["result"]["count"] == 1 and second["result"]["count"] == 2
    assert [n["content"] for n in second["result"]["notes"]] == ["note a", "note b"]
    saved = app.schema.t("runtime_session_state")
    assert len(saved) == 1 and saved[0]["flow"] == {"count": 2} and saved[0]["version"] == 2


async def test_E19_session_variables_reach_the_next_turn_and_node_outputs_do_not() -> None:
    app = make_app(workflows={"wf-chat": MEMORY})
    async with app.client() as client:
        first = (await client.post("/v1/runs", json=sync("a"), headers=auth())).json()
        second = (
            await client.post("/v1/runs", json=sync("b", sessionId=first["sessionId"]), headers=auth())
        ).json()
    assert second["result"]["count"] == 2  # the declared variable carried over
    run = app.schema.run(second["runId"])
    assert run is not None
    # Node records and visit counts start empty: the node's visit this turn is its first.
    assert [(e["nodeId"], e["visit"]) for e in run["node_timeline"]] == [("start", 1), ("v", 1), ("out", 1)]


# --- A-07, A-08 ------------------------------------------------------------------------------


async def test_A08_sync_fast_answers_200_and_slow_answers_202_with_links() -> None:
    app = make_app([Turn.say("Quick."), Turn.say("Slow.", delay=0.5)], runs={"sync_wait_seconds": 0.1})
    async with app.client() as client:
        fast = await client.post("/v1/runs", json=sync("one"), headers=auth())
        slow = await client.post("/v1/runs", json=sync("two"), headers=auth())
        assert fast.status_code == 200 and fast.json()["result"] == {"answer": "Quick."}
        assert slow.status_code == 202 and slow.json()["links"]["self"] == f"/v1/runs/{slow.json()['runId']}"
        await app.deps.registry.wait(slow.json()["runId"], 5)
        done = (await client.get(slow.json()["links"]["self"], headers=auth())).json()
    assert done["status"] == "succeeded" and done["result"] == {"answer": "Slow."}


async def test_A07_one_active_run_per_session_is_enforced_by_the_start_batch() -> None:
    app = make_app([Turn.say("Hold.", delay=0.5), Turn.say("Next.")], runs={"sync_wait_seconds": 0.05})
    async with app.client() as client:
        first = (await client.post("/v1/runs", json=sync("one"), headers=auth())).json()
        busy = await client.post("/v1/runs", json=sync("two", sessionId=first["sessionId"]), headers=auth())
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "RUN_IN_PROGRESS"
    assert CHAT["name"] == "chat"
