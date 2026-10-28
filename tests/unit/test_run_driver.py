"""Run driver and event stream (P2-01: S-02, S-03, S-05, S-11, S-12, S-13, E-17, R-06)."""

from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import replace
from typing import Any

import pytest
from pydantic import TypeAdapter

from tests.fakes.clock import FakeClock
from tests.fakes.scripted_chat_model import ScriptedChatModel, Turn
from tests.kinds.harness import (
    AskKind,
    EchoKind,
    FailingKind,
    Harness,
    Recorder,
    ResultKind,
    SlowKind,
    with_checkpointer,
)
from tests.kinds.streaming import DriverHarness, DriverRun, TalkKind, ToolAgentKind
from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import RuntimeResources, load_resources
from zonic_runtime.engine.kinds.base import BaseKind
from zonic_runtime.engine.streaming.v2_mapper import V2EventSource
from zonic_runtime.errors import ZonicError
from zonic_runtime.models.blueprint import Blueprint, NodeSpec
from zonic_runtime.models.engine import NodeOutcome
from zonic_runtime.models.events import EventDraft, RunEvent
from zonic_runtime.run.driver import ActiveRun, RunOutcome
from zonic_runtime.run.events.bus import RunEventBus
from zonic_runtime.run.events.summarizer import CUT, cap_data, summarize

DEV = load_resources(EnvironmentModel(env="dev"))
MESSAGES = [{"role": "user", "content": "{{system.userQuery}}"}]
EVENT = TypeAdapter(RunEvent)
UNICODE = "Grüße 👋🏽 aus Zürich —\nzweite Zeile: 日本語のテキスト\r\n\ttab ✓"


def resources(**runs: Any) -> RuntimeResources:
    settings = DEV.settings.model_copy(update={"runs": DEV.settings.runs.model_copy(update=runs)})
    return replace(DEV, settings=settings)


def chain(*nodes: dict[str, Any]) -> dict[str, Any]:
    """Start, then each node in order."""
    ids = ["start", *(n["id"] for n in nodes)]
    return {
        "schemaVersion": "2.0",
        "nodes": [{"id": "start", "type": "start", "config": {}}, *nodes],
        "edges": [{"source": a, "target": b} for a, b in itertools.pairwise(ids)],
    }


def llm(node_id: str = "n") -> dict[str, Any]:
    return {"id": node_id, "type": "llm", "name": node_id, "config": {"messages": MESSAGES}}


def agent(node_id: str = "ag") -> dict[str, Any]:
    return {"id": node_id, "type": "agent", "name": node_id, "config": {}}


def output(node_id: str = "out") -> dict[str, Any]:
    return {"id": node_id, "type": "output", "config": {}}


def harness(
    script: list[Turn] | None = None,
    *,
    kinds: dict[str, Any] | None = None,
    base: RuntimeResources = DEV,
    source: Any = None,
    finisher: Any = None,
) -> tuple[DriverHarness, Recorder]:
    recorder = Recorder()
    model = ScriptedChatModel(script=list(script or []))
    factories = {
        "llm": lambda s, b: TalkKind(s, b, recorder, model),
        "agent": lambda s, b: ToolAgentKind(s, b, recorder, model),
        "output": lambda s, b: ResultKind(s, b, recorder),
        **(kinds or {}),
    }
    return DriverHarness(
        Harness(base, factories, recorder=recorder), source=source, finisher=finisher
    ), recorder


def normalized(run: DriverRun) -> list[dict[str, Any]]:
    """The events without the volatile fields (IDs, times, durations)."""
    out = []
    for event in run.events:
        data = {k: v for k, v in event["data"].items() if k != "durationMs"}
        out.append({**{k: v for k, v in event.items() if k not in ("id", "ts", "data")}, "data": data})
    return out


def compact(types: list[str]) -> list[str]:
    """Consecutive token deltas as one."""
    return [t for i, t in enumerate(types) if not (t == "token.delta" and i and types[i - 1] == t)]


# --- S-02, S-03, S-05, S-11 ------------------------------------------------------------------------


async def test_S02_start_llm_output_event_sequence() -> None:
    h, _ = harness([Turn.say("The answer is 42.")])
    run = await h.run(chain(llm(), output()))
    assert run.outcome.status == "succeeded" and run.outcome.result == {"answer": "out"}
    lifecycle = [(e["type"], e.get("nodeId")) for e in run.events if e["type"] != "token.delta"]
    assert lifecycle == [
        ("run.started", None),
        ("node.started", "start"),
        ("node.finished", "start"),
        ("node.started", "n"),
        ("node.finished", "n"),
        ("node.started", "out"),
        ("node.finished", "out"),
        ("run.completed", None),
    ]
    assert compact(run.types())[3:6] == ["node.started", "token.delta", "node.finished"]
    assert {e.get("nodeId") for e in run.events if e["type"] == "token.delta"} == {"n"}
    completed = run.events[-1]["data"]
    assert completed["result"] == {"answer": "out"} and completed["usage"]["totalTokens"] == 3
    started = run.events[0]["data"]
    assert (
        started["agenticWorkflowId"] == "wf"
        and started["sessionId"] == "s-1"
        and len(started["revision"]) == 64
    )


async def test_S03_token_deltas_arrive_before_the_run_ends_and_join_to_the_answer() -> None:
    text = "Streaming arrives word by word, before the run ends."
    h, _ = harness([Turn.say(text, chunk_delay=0.005)])
    run, graph = h.prepare(chain(llm(), output()))
    driving = asyncio.ensure_future(h.driver().drive(run, graph, {}))
    seen: list[tuple[str, bool]] = []
    async for event in run.stream.subscribe(1):
        seen.append((event["type"], driving.done()))
    outcome = await driving
    deltas = [done for kind, done in seen if kind == "token.delta"]
    assert deltas and not any(deltas)  # every delta reached the subscriber while the run was live
    finished = [e async for e in run.stream.subscribe(1)]
    assert "".join(e["data"]["text"] for e in finished if e["type"] == "token.delta") == text
    assert outcome.state is not None and outcome.state["nodes"]["n"]["output"]["text"] == text


async def test_S05_seq_is_strictly_increasing_without_gaps_and_every_event_matches_its_model() -> None:
    h, _ = harness(
        [Turn.call("lookup", {"city": "Oslo"}), Turn.say("Sunny in Oslo."), Turn.say("Done.")],
    )
    run = await h.run(chain(agent(), llm(), output()))
    seqs = [e["seq"] for e in run.events]
    assert seqs == list(range(1, len(seqs) + 1))
    assert len({e["id"] for e in run.events}) == len(seqs)
    for event in run.events:
        EVENT.validate_python(event)
    assert run.stream.last_seq == seqs[-1]


async def test_S11_unicode_and_multiline_deltas_are_intact() -> None:
    chunks = ("Grüße 👋🏽 ", "aus Zürich —\n", "zweite Zeile: 日本語", "のテキスト\r\n\ttab ✓")
    h, _ = harness([Turn(text="".join(chunks), chunks=chunks)])
    run = await h.run(chain(llm(), output()))
    deltas = [e["data"]["text"] for e in run.events if e["type"] == "token.delta"]
    assert deltas == list(chunks) and "".join(deltas) == UNICODE
    capped, truncated = cap_data({"text": UNICODE * 100}, 512)
    assert truncated and capped["text"].endswith(CUT)
    assert (UNICODE * 100).startswith(capped["text"][: -len(CUT)])


# --- tools, node events ------------------------------------------------------------------------------


async def test_S02_tool_calls_of_an_agent_become_tool_events_with_its_node_id() -> None:
    h, _ = harness(
        [Turn.calls(("lookup", {"city": "Oslo"}), ("broken", {"city": "Bergen"})), Turn.say("Mixed.")]
    )
    run = await h.run(chain(agent(), output()))
    tools = [e for e in run.events if e["type"].startswith("tool.")]
    assert {e["nodeId"] for e in tools} == {"ag"} and all("path" not in e for e in tools)
    started = {e["data"]["toolUseId"]: e["data"] for e in tools if e["type"] == "tool.started"}
    finished = {e["data"]["toolUseId"]: e["data"] for e in tools if e["type"] == "tool.finished"}
    assert sorted(d["toolName"] for d in started.values()) == ["broken", "lookup"]
    assert started.keys() == finished.keys()
    by_name = {started[i]["toolName"]: finished[i]["status"] for i in started}
    assert by_name == {"lookup": "success", "broken": "error"}
    assert all(
        d["inputSummary"] == {"type": "object", "fields": 1, "bytes": d["inputSummary"]["bytes"]}
        for d in started.values()
    )
    assert run.text("ag") == "Mixed."


async def test_S02_node_finished_carries_an_output_summary_never_the_output() -> None:
    h, _ = harness([Turn.say("secret answer")])
    run = await h.run(chain(llm(), output()))
    finished = next(e for e in run.events if e["type"] == "node.finished" and e["nodeId"] == "n")
    assert "output" not in finished["data"]
    assert finished["data"]["outputSummary"]["type"] == "object"
    assert "secret" not in str(finished["data"]["outputSummary"])
    started = next(e for e in run.events if e["type"] == "node.started" and e["nodeId"] == "n")
    assert started["data"] == {"nodeType": "llm", "nodeName": "n", "visit": 1, "resumed": False}


async def test_S02_warnings_and_state_updates_are_events_of_their_node() -> None:
    target = {
        "id": "v",
        "type": "variable",
        "config": {"variableUpdates": [{"path": "flow.topic", "value": "{{flow.missing}}"}]},
    }
    recorder = Recorder()
    h = DriverHarness(Harness(DEV, {"variable": lambda s, b: EchoKind(s, b, recorder)}, recorder=recorder))
    run = await h.run(chain(target))
    mine = [e for e in run.events if e.get("nodeId") == "v"]
    assert next(e["type"] for e in mine) == "node.started" and mine[-1]["type"] == "node.finished"
    assert {"type": "state.updated", "keys": ["topic"]} in [
        {"type": e["type"], **e["data"]} for e in mine if e["type"] == "state.updated"
    ]
    assert any(e["type"] == "warning" and e["data"]["code"] == "TEMPLATE_MISSING" for e in mine)


# --- S-12: the v2 fallback -------------------------------------------------------------------------


def _parallel() -> dict[str, Any]:
    return {
        "schemaVersion": "2.0",
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            llm("left"),
            {"id": "right", "type": "variable", "config": {"errorHandling": {"policy": "continue"}}},
            output(),
        ],
        "edges": [
            {"source": "start", "target": "left"},
            {"source": "start", "target": "right"},
            {"source": "left", "target": "out"},
            {"source": "right", "target": "out"},
        ],
    }


SCENARIOS = {
    "llm": (lambda: chain(llm(), output()), [Turn.say("Hello there, world.")]),
    "agent-tools": (
        lambda: chain(agent(), llm(), output()),
        # One call per turn: two calls in one turn run concurrently, in no fixed order.
        [
            Turn.call("lookup", {"city": "Oslo"}),
            Turn.call("broken", {"city": "Rome"}),
            Turn.say("Two tools."),
            Turn.say("Bye."),
        ],
    ),
    "parallel-continue": (_parallel, [Turn.say("Left branch text.")]),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_S12_the_v2_fallback_publishes_the_same_events_as_v3(name: str) -> None:
    build, script = SCENARIOS[name]
    failing = {"variable": lambda s, b: FailingKind(s, b, Recorder(), "TOOL_UNAVAILABLE")}
    v3, _ = harness(list(script), kinds=failing)
    v2, _ = harness(list(script), kinds=failing, source=V2EventSource())
    first, second = await v3.run(build()), await v2.run(build())
    assert first.outcome.status == second.outcome.status == "succeeded"
    assert normalized(first) == normalized(second)
    assert any(e["type"] == "token.delta" for e in first.events)


# --- S-13 (DR-9) --------------------------------------------------------------------------------------


async def test_S13_seq_continues_from_last_event_seq() -> None:
    h, _ = harness([Turn.say("Resumed.")])
    run = await h.run(chain(llm(), output()), start_seq=41)
    seqs = [e["seq"] for e in run.events if e["type"] != "stream.gap"]
    assert seqs[0] == 42 and seqs == list(range(42, 42 + len(seqs)))
    assert run.events[0]["type"] == "stream.gap" and run.events[0]["data"] == {"fromSeq": 1, "toSeq": 41}
    later = [e async for e in run.stream.subscribe(42)]
    assert [e["seq"] for e in later] == seqs


# --- E-17: deadline and cancellation ------------------------------------------------------------------


async def test_E17_the_run_deadline_ends_the_run_timed_out() -> None:
    recorder = Recorder()
    h = DriverHarness(Harness(DEV, {"llm": lambda s, b: SlowKind(s, b, recorder, 30)}, recorder=recorder))
    run = await h.run(chain(llm(), output()), seconds=0.05)
    assert run.outcome.status == "timed_out" and run.outcome.error and run.outcome.error.code == "RUN_TIMEOUT"
    assert run.events[-1]["type"] == "run.failed" and run.events[-1]["data"]["code"] == "RUN_TIMEOUT"
    assert recorder.cancelled == ["n"]


async def test_E17_cancel_during_a_model_stream_ends_cancelled_within_the_grace_period() -> None:
    h, recorder = harness(
        [Turn.say("word " * 200, chunk_delay=0.01)], base=resources(cancel_grace_seconds=2.0)
    )
    run, graph = h.prepare(chain(llm(), output()))
    driving = asyncio.ensure_future(h.driver().drive(run, graph, {}))
    async for event in run.stream.subscribe(1):
        if event["type"] == "token.delta":
            break
    cancelled_at = time.monotonic()
    run.cancel()
    outcome = await driving
    assert time.monotonic() - cancelled_at < 2.0
    assert outcome.status == "cancelled" and outcome.error and outcome.error.code == "CANCELLED"
    assert recorder.cancelled == ["n"] and "out" not in recorder.calls
    events = [e async for e in run.stream.subscribe(1)]
    assert events[-1]["type"] == "run.cancelled" and events[-1]["data"] == {"reason": "cancelled"}


class StubbornKind(BaseKind):
    """Ignores cancellation, as a badly written blocking integration would."""

    def __init__(self, spec: NodeSpec, blueprint: Blueprint, recorder: Recorder) -> None:
        self.spec, self.recorder = spec, recorder

    async def execute(self, ctx: Any, node_input: Any) -> NodeOutcome:
        self.recorder.started.set()
        for _ in range(10):
            try:
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                self.recorder.cancelled.append(self.spec.node_id)
        return NodeOutcome(activate=["default"])


async def test_E17_a_node_that_ignores_cancellation_cannot_hold_the_run_past_the_grace_period() -> None:
    recorder = Recorder()
    h = DriverHarness(
        Harness(
            resources(cancel_grace_seconds=0.1),
            {"llm": lambda s, b: StubbornKind(s, b, recorder)},
            recorder=recorder,
        )
    )
    run, graph = h.prepare(chain(llm(), output()))
    driving = asyncio.ensure_future(h.driver().drive(run, graph, {}))
    await recorder.started.wait()
    began = time.monotonic()
    run.cancel()
    outcome = await driving
    assert outcome.status == "cancelled" and time.monotonic() - began < 0.1 + 0.4
    assert recorder.cancelled == ["n"]
    await asyncio.sleep(0.6)  # let the stubborn node finish before the loop closes


# --- other outcomes -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "code", "status"),
    [
        (lambda s, b, r: FailingKind(s, b, r, "TOOL_UNAVAILABLE"), "TOOL_UNAVAILABLE", "failed"),
        (lambda s, b, r: FailingKind(s, b, r, "CANCELLED"), "CANCELLED", "cancelled"),
        (lambda s, b, r: FailingKind(s, b, r, "RUN_TIMEOUT"), "RUN_TIMEOUT", "timed_out"),
        (lambda s, b, r: FailingKind(s, b, r, "NOT_IN_THE_CATALOG"), "INTERNAL", "failed"),
    ],
)
async def test_E17_exceptions_map_to_the_run_outcome(kind: Any, code: str, status: str) -> None:
    recorder = Recorder()
    h = DriverHarness(Harness(DEV, {"variable": lambda s, b: kind(s, b, recorder)}, recorder=recorder))
    run = await h.run(chain({"id": "v", "type": "variable", "config": {}}, output()))
    assert run.outcome.status == status and run.outcome.error and run.outcome.error.code == code
    assert run.events[-1]["type"] == ("run.cancelled" if status == "cancelled" else "run.failed")


async def test_E17_loop_limits_and_a_missing_output_fail_the_run() -> None:
    from tests.kinds.harness import answer_after, policy_kinds
    from tests.unit.test_loops import loop_document

    recorder = Recorder()
    kinds = sorted({e.kind for e in DEV.manifest.node_types.values()})
    h = DriverHarness(Harness(DEV, policy_kinds(kinds, recorder, answer_after(10**6)), recorder=recorder))
    run = await h.run(loop_document())
    assert run.outcome.error and run.outcome.error.code == "LOOP_LIMIT_EXCEEDED"
    h2 = DriverHarness(Harness(DEV, {}))
    run2 = await h2.run(chain({"id": "v", "type": "variable", "config": {}}))
    assert run2.outcome.error and run2.outcome.error.code == "NO_OUTPUT"
    assert run2.events[-1]["data"] == {
        "code": "NO_OUTPUT",
        "message": DEV.errors.errors["NO_OUTPUT"].message,
        "retryable": False,
    }


async def test_E17_the_terminal_event_follows_the_save_and_a_failed_save_fails_the_run() -> None:
    order: list[str] = []

    async def save(run: ActiveRun, outcome: RunOutcome) -> RunOutcome:
        order.append(f"save:{run.stream.last_seq}")
        raise ZonicError("STATE_UNAVAILABLE")

    h, _ = harness([Turn.say("Saved?")], finisher=save)
    run = await h.run(chain(llm(), output()))
    assert order == [f"save:{run.events[-2]['seq']}"]  # nothing published between save and terminal
    assert run.outcome.status == "failed" and run.events[-1]["type"] == "run.failed"
    assert run.events[-1]["data"]["code"] == "STATE_UNAVAILABLE" and run.events[-1]["data"]["retryable"]


async def test_E17_an_interrupt_ends_the_turn_waiting_with_its_request() -> None:
    recorder = Recorder()
    h = DriverHarness(
        with_checkpointer(
            Harness(DEV, {"interrupt": lambda s, b: AskKind(s, b, recorder)}, recorder=recorder)
        )
    )
    ask = {"id": "ask", "type": "human_input", "config": {"prompt": "answer?"}}
    run = await h.run(chain(ask, output()))
    assert run.outcome.status == "waiting_input" and len(run.outcome.interrupts) == 1
    requested, waiting = run.events[-2], run.events[-1]
    assert requested["type"] == "interrupt.requested" and requested["data"]["prompt"] == "answer?"
    assert requested["data"]["kind"] == "input"
    assert waiting == {
        **waiting,
        "type": "run.waiting",
        "data": {"interruptIds": [requested["data"]["interruptId"]]},
    }


class DrainKind(BaseKind):
    def __init__(self, spec: NodeSpec, blueprint: Blueprint, holder: dict[str, ActiveRun]) -> None:
        self.spec, self.holder = spec, holder

    async def execute(self, ctx: Any, node_input: Any) -> NodeOutcome:
        self.holder["run"].control.request_drain("shutdown")
        return NodeOutcome(activate=["default"])


async def test_E17_a_drained_run_ends_without_a_terminal_event() -> None:
    holder: dict[str, ActiveRun] = {}
    recorder = Recorder()
    kinds = {
        "variable": lambda s, b: DrainKind(s, b, holder),
        "output": lambda s, b: ResultKind(s, b, recorder),
    }
    h = DriverHarness(with_checkpointer(Harness(DEV, kinds, recorder=recorder)))
    run, graph = h.prepare(chain({"id": "v", "type": "variable", "config": {}}, output()))
    holder["run"] = run
    result = await h.drive(run, graph)
    assert result.outcome.status == "drained" and recorder.calls == []
    assert result.events[-1]["type"] == "node.finished" and run.stream.closed


# --- R-06 and the bus ----------------------------------------------------------------------------------


async def test_R06_replay_buffers_are_bounded_and_released_after_retention() -> None:
    clock = FakeClock()
    settings = DEV.settings.stream.model_copy(
        update={"replay_buffer_events": 50, "replay_retention_seconds": 60}
    )
    bus = RunEventBus(settings, DEV.settings.sse.max_event_bytes, clock=clock)
    streams = [bus.create(f"run-{i}") for i in range(20)]
    for stream in streams:
        for i in range(500):
            stream.publish(EventDraft(type="token.delta", data={"text": f"t{i}"}, node_id="n"))
    assert all(s.size == 50 and s.oldest_seq == 451 and s.last_seq == 500 for s in streams)
    for stream in streams[:15]:
        stream.close()
    clock.advance(59)
    assert bus.size == 20 and bus.active == 5
    clock.advance(1)
    assert bus.get("run-0") is None and bus.size == 5 and bus.active == 5


async def test_R06_a_subscriber_behind_the_buffer_gets_a_gap_and_the_run_never_waits() -> None:
    settings = DEV.settings.stream.model_copy(update={"replay_buffer_events": 10})
    stream = RunEventBus(settings, DEV.settings.sse.max_event_bytes).create("run-1")
    reader = stream.subscribe(1)
    stream.publish(EventDraft(type="run.started", data={}))
    first = await anext(reader)
    for i in range(30):  # the subscriber reads nothing meanwhile
        stream.publish(EventDraft(type="token.delta", data={"text": str(i)}))
    stream.close()
    rest = [e async for e in reader]
    assert first["seq"] == 1 and rest[0]["type"] == "stream.gap"
    assert rest[0]["data"] == {"fromSeq": 2, "toSeq": 21} and [e["seq"] for e in rest[1:]] == list(
        range(22, 32)
    )


async def test_R06_an_event_above_max_event_bytes_is_cut_and_marked() -> None:
    stream = RunEventBus(DEV.settings.stream, 256).create("run-1")
    event = stream.publish(
        EventDraft(
            type="warning", data={"code": "BIG", "message": "x" * 5000, "details": {"rows": list(range(500))}}
        )
    )
    assert event["truncated"] is True and len(event["data"]["message"]) < 100
    assert event["data"]["code"] == "BIG" and isinstance(event["data"]["details"], str)


def test_summaries_hold_types_and_sizes_only() -> None:
    assert summarize({"name": "Alice", "ssn": "123"}) == {"type": "object", "fields": 2, "bytes": 28}
    assert summarize([1, 2, 3]) == {"type": "array", "length": 3, "bytes": 7}
    assert summarize("secret") == {"type": "string", "length": 6}
    assert [summarize(v)["type"] for v in (None, True, 1.5)] == ["null", "boolean", "number"]
