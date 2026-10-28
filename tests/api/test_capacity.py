"""Admission, load shedding and the process's gauges through the app (P2-05: Q-01, Q-02, Q-05,
Q-15, R-04, ``/metrics``). The budgets on the target task are P5-02's (k6)."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from tests.api.app import auth, make_app, sse_events, token
from tests.api.test_persistence import wide
from tests.fakes.clock import FakeClock
from tests.fakes.scripted_chat_model import Turn
from zonic_runtime.api.internal import create_internal_app
from zonic_runtime.api.sse import sse_frames
from zonic_runtime.models.api.requests import StartRunRequest
from zonic_runtime.models.events import EventDraft
from zonic_runtime.run.events.bus import RunStream
from zonic_runtime.runtime.pressure import PressureMonitor


def start(text: str = "hello", mode: str = "stream", **extra: Any) -> dict[str, Any]:
    return {"agenticWorkflowId": "wf-chat", "input": {"text": text}, "mode": mode, **extra}


# --- Q-01, Q-02 --------------------------------------------------------------------------------


async def test_Q01_a_warm_run_start_makes_one_gateway_round_trip_before_run_started() -> None:
    app = make_app([Turn.say("First."), Turn.say("Second.", delay=0.2)])
    async with app.client() as client:
        first = (await client.post("/v1/runs", json=start(mode="sync"), headers=auth())).json()
    caller = await app.deps.verifier.verify(token())
    app.gateway.calls.clear()
    request = StartRunRequest.model_validate(start(sessionId=first["sessionId"]))
    started = await app.deps.runs.start(caller, request)
    event = await anext(started.events)  # type: ignore[union-attr]
    before = [(c.kind, c.name) for c in app.gateway.calls]
    await app.deps.registry.wait(started.run_id, 5)  # type: ignore[union-attr]
    assert event["type"] == "run.started"
    assert before == [("batch:sequential", "v_session_state,v_message,sp_run_start")]


async def test_Q02_a_200_node_linear_workflow_calls_the_database_only_at_start_and_end() -> None:
    document = wide(198)  # with Start and Output: 200 nodes
    assert len(document["nodes"]) == 200
    app = make_app(workflows={"wf-chat": document})
    async with app.client() as client:
        first = (await client.post("/v1/runs", json=start("first", mode="sync"), headers=auth())).json()
        app.gateway.calls.clear()
        body = start("second", mode="sync", sessionId=first["sessionId"])
        second = (await client.post("/v1/runs", json=body, headers=auth())).json()
    assert first["status"] == second["status"] == "succeeded" and second["result"] == {"count": True}
    assert [(c.kind, c.name) for c in app.gateway.calls] == [
        ("batch:sequential", "v_session_state,v_message,sp_run_start"),
        ("procedure", "sp_run_finish"),
    ]


# --- R-01 --------------------------------------------------------------------------------------


def _stall() -> Turn:
    """A stream that stops after two chunks until botocore's read timeout (the Bedrock client's
    ``read_timeout`` is ``models.requestTimeoutSeconds``: the longest gap between bytes)."""
    from botocore.exceptions import ReadTimeoutError

    return Turn.say(
        "partial answer then silence",
        error=ReadTimeoutError(endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com"),
        error_after_chunks=2,
    )


async def test_R01_a_stalled_model_stream_is_retried_and_the_turn_completes() -> None:
    from tests.conformance.test_kinds import fast_retry

    app = make_app([_stall(), Turn.say("Recovered answer.")], resources=fast_retry())
    async with app.client() as client:
        response = await client.post("/v1/runs", json=start(), headers=auth())
    events = sse_events(response.text)
    assert events[-1]["type"] == "run.completed", events[-1]
    assert events[-1]["data"]["result"] == {"answer": "Recovered answer."}
    assert len(app.models.model.requests) == 2


async def test_R01_a_model_that_keeps_stalling_fails_the_run_with_model_unavailable() -> None:
    from tests.conformance.test_kinds import fast_retry

    resources = fast_retry()
    retry = resources.manifest.node_types["llm"].retry
    assert retry is not None
    app = make_app([_stall()] * retry.max_attempts, resources=resources)
    async with app.client() as client:
        response = await client.post("/v1/runs", json=start(), headers=auth())
    events = sse_events(response.text)
    assert events[-1]["type"] == "run.failed" and events[-1]["data"]["code"] == "MODEL_UNAVAILABLE"
    assert len(app.models.model.requests) == retry.max_attempts


# --- Q-05 --------------------------------------------------------------------------------------


async def test_Q05_150_concurrent_streaming_runs_never_cross_and_the_loop_is_not_overloaded() -> None:
    def answer(request: Any) -> Turn:
        return Turn.say(f"Answer for {request.last_text}", chunk_delay=0.002)

    runs = 150
    app = make_app([answer] * runs)
    settings = app.deps.resources.settings
    monitor = PressureMonitor(settings.pressure.model_copy(update={"sample_interval_milliseconds": 20}))
    monitor.start()
    try:
        async with app.client() as client:
            responses = await asyncio.gather(
                *(
                    client.post(
                        "/v1/runs", json=start(f"question {i}"), headers=auth(f"u{i}", tenant=f"t{i}")
                    )
                    for i in range(runs)
                )
            )
    finally:
        await monitor.stop()
    assert all(r.status_code == 200 for r in responses), [
        (r.status_code, r.text[:200]) for r in responses if r.status_code != 200
    ][:3]
    for i, response in enumerate(responses):
        events = sse_events(response.text)
        assert len({e["runId"] for e in events}) == 1
        assert events[-1]["type"] == "run.completed"
        assert events[-1]["data"]["result"] == {"answer": f"Answer for question {i}"}
    # What overloaded() judges: one GC pause or the burst of 150 starts is a single slow sample. The
    # p99 budget (50 ms) is P5-02's, measured with k6 on the target task.
    p90 = monitor.percentile(0.9)
    detail = f"event-loop lag p90 {p90:.0f} ms, p99 {monitor.percentile(0.99):.0f} ms"
    assert p90 <= settings.pressure.max_event_loop_lag_milliseconds, detail
    assert app.deps.executor.active == 0


async def test_Q05_a_slow_sse_reader_falls_behind_alone_and_its_pump_ends_with_it() -> None:
    settings = make_app().deps.resources.settings
    stream = RunStream(
        "r", settings.stream.model_copy(update={"replay_buffer_events": 10}), settings.sse.max_event_bytes
    )
    frames = sse_frames(stream.subscribe(1), "r", 15, FakeClock(), queue_events=2)
    stream.publish(EventDraft(type="run.started", data={}))
    assert b"run.started" in await anext(frames)
    for i in range(50):  # the run publishes without waiting for the reader
        stream.publish(EventDraft(type="node.started", data={"visit": i}, node_id="n", path="n"))
    stream.close()
    rest = [f.decode() async for f in frames]
    assert any("event: stream.gap" in f for f in rest) and rest[-1].startswith("id: 51")

    tasks = len(asyncio.all_tasks())
    other = RunStream("q", settings.stream, settings.sse.max_event_bytes)
    for i in range(5):
        other.publish(EventDraft(type="node.started", data={"visit": i}, node_id="n", path="n"))
    reader = sse_frames(other.subscribe(1), "q", 15, FakeClock(), queue_events=1)
    await anext(reader)
    await asyncio.sleep(0.01)  # the pump now waits on its full queue
    await reader.aclose()  # the client went away: the pump is cancelled and nothing is left behind
    await asyncio.sleep(0.01)
    assert len(asyncio.all_tasks()) == tasks


# --- R-04, Q-15 --------------------------------------------------------------------------------


async def test_R04_at_max_concurrent_runs_a_new_run_is_429_and_the_running_run_is_unaffected() -> None:
    app = make_app([Turn.say("Slow answer.", delay=0.3), Turn.say("Later.")], runs={"max_concurrent_runs": 1})
    caller = await app.deps.verifier.verify(token())
    running = await app.deps.runs.start(caller, StartRunRequest.model_validate(start()))
    async with app.client() as client:
        refused = await client.post("/v1/runs", json=start(mode="sync"), headers=auth("bob"))
        outcome = await app.deps.registry.wait(running.run_id, 5)  # type: ignore[union-attr]
        later = await client.post("/v1/runs", json=start(mode="sync"), headers=auth("bob"))
    assert refused.status_code == 429 and refused.headers["retry-after"] == "1"
    assert refused.json()["error"]["code"] == "RATE_LIMITED"
    assert outcome is not None and outcome.status == "succeeded"
    assert later.status_code == 200 and later.json()["result"] == {"answer": "Later."}
    assert app.deps.executor.active == 0


async def test_R04_a_start_that_fails_before_its_run_exists_gives_its_place_back() -> None:
    app = make_app(runs={"max_concurrent_runs": 1})
    async with app.client() as client:
        missing = await client.post(
            "/v1/runs", json={**start(mode="sync"), "agenticWorkflowId": "wf-none"}, headers=auth()
        )
    assert missing.status_code == 404 and app.deps.executor.active == 0


async def test_Q15_an_overloaded_process_refuses_new_runs_with_503_and_readiness_is_degraded() -> None:
    app = make_app([Turn.say("Fine again.")])
    pressure = app.deps.pressure
    pressure._rss = lambda: pressure._settings.max_rss_megabytes + 1  # type: ignore[method-assign]
    async with app.client() as client:
        refused = await client.post("/v1/runs", json=start(mode="sync"), headers=auth())
        ready = await client.get("/readyz")
        pressure._rss = lambda: 0.0  # type: ignore[method-assign]
        accepted = await client.post("/v1/runs", json=start(mode="sync"), headers=auth())
        healthy = await client.get("/readyz")
    assert refused.status_code == 503 and refused.json()["error"]["code"] == "OVERLOADED"
    assert refused.headers["retry-after"] == "1"
    assert ready.status_code == 200 and ready.json()["status"] == "degraded"
    assert ready.json()["checks"] == {"gateway": "ok", "pressure": "degraded"}
    assert accepted.status_code == 200 and healthy.json()["status"] == "ok"


# --- /metrics ----------------------------------------------------------------------------------


async def test_metrics_on_the_internal_app_report_runs_permits_lag_and_model_governors() -> None:
    app = make_app([Turn.say("Counted.")])
    async with app.client() as client:
        await client.post("/v1/runs", json=start(mode="sync"), headers=auth())
    transport = httpx.ASGITransport(app=create_internal_app(app.deps))
    async with httpx.AsyncClient(transport=transport, base_url="http://internal.test") as internal:
        response = await internal.get("/metrics")
    text = response.text
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/plain")
    for gauge in (
        "zonic_active_runs 0.0",
        "zonic_node_permits_in_use 0.0",
        "zonic_event_loop_lag_milliseconds",
        "zonic_overloaded 0.0",
        "zonic_resident_memory_megabytes",
    ):
        assert gauge in text
    model = app.deps.resources.models.default_model
    assert f'zonic_model_in_flight{{model="{model}"}} 0.0' in text
    assert f'zonic_model_queue_depth{{model="{model}"}} 0.0' in text


def test_Q15_one_slow_sample_does_not_shed_runs_but_sustained_lag_does() -> None:
    settings = make_app().deps.resources.settings.pressure
    monitor = PressureMonitor(settings, rss=lambda: 0.0)
    limit = settings.max_event_loop_lag_milliseconds
    monitor._lags.extend([5.0] * 9 + [limit * 3])  # one GC pause
    assert not monitor.overloaded()
    monitor._lags.extend([limit * 2] * 3)  # lag that stays
    assert monitor.overloaded()
