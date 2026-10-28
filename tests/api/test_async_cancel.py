"""The ``async`` mode and cancellation (P4-04: A-08, A-13, H-12, E-17), through the app.

``async`` answers ``202`` with the run's links at once; the run goes on, and ``GET /v1/runs/{id}``
answers with an ``ETag`` (``304`` while unchanged) and ``Retry-After`` while it runs.
``POST /v1/runs/{id}/cancel`` stops a run this instance drives, and moves a waiting run to
``cancelled`` with its interrupts.
"""

from __future__ import annotations

import asyncio
from typing import Any

from tests.api.app import auth, make_app, sse_events
from tests.api.test_interrupts import APPROVAL, DRAFT, FLOW, paused
from tests.fakes.scripted_chat_model import Turn
from zonic_runtime.errors import ZonicError


def start(mode: str, text: str = "hello") -> dict[str, Any]:
    return {"agenticWorkflowId": "wf-chat", "input": {"text": text}, "mode": mode}


# --- A-08 ---------------------------------------------------------------------------------------


async def test_A08_async_answers_202_at_once_and_the_run_then_succeeds() -> None:
    app = make_app([Turn.say("Done later.", delay=0.3)])
    async with app.client() as client:
        accepted = await client.post("/v1/runs", json=start("async"), headers=auth())
        body = accepted.json()
        assert accepted.status_code == 202 and body["status"] == "running"
        assert body["links"] == {
            "self": f"/v1/runs/{body['runId']}",
            "events": f"/v1/runs/{body['runId']}/events",
        }
        running = await client.get(body["links"]["self"], headers=auth())
        assert running.json()["status"] == "running" and running.headers["Retry-After"] == "1"
        unchanged = await client.get(
            body["links"]["self"], headers={**auth(), "If-None-Match": running.headers["ETag"]}
        )
        assert unchanged.status_code == 304
        await app.deps.registry.wait(body["runId"], 5)
        done = await client.get(
            body["links"]["self"], headers={**auth(), "If-None-Match": running.headers["ETag"]}
        )
    assert done.status_code == 200 and done.json()["status"] == "succeeded"
    assert done.json()["result"] == {"answer": "Done later."} and "Retry-After" not in done.headers


async def test_A08_an_async_answer_resumes_the_run_without_waiting() -> None:
    app = make_app([Turn.say(DRAFT)], workflows={FLOW: APPROVAL})
    async with app.client() as client:
        run_id, interrupt_id, _ = await paused(app, client)
        answer = {"answer": {"approved": True}, "mode": "async"}
        accepted = await client.post(
            f"/v1/runs/{run_id}/interrupts/{interrupt_id}", json=answer, headers=auth()
        )
        assert accepted.status_code == 202 and accepted.json()["links"]["self"] == f"/v1/runs/{run_id}"
        await app.deps.registry.wait(run_id, 5)
        done = (await client.get(f"/v1/runs/{run_id}", headers=auth())).json()
    assert done["status"] == "succeeded" and done["result"] == {"response": DRAFT}


# --- A-13 ---------------------------------------------------------------------------------------


async def test_A13_readyz_names_the_gateway_when_it_is_down() -> None:
    app = make_app()

    async def down() -> bool:
        return False

    app.gateway.ready = down  # type: ignore[method-assign]
    async with app.client() as client:
        ready = await client.get("/readyz")
    assert ready.status_code == 503
    assert ready.json()["status"] == "not_ready" and ready.json()["checks"]["gateway"] == "unavailable"


async def test_A13_readyz_stays_ready_with_tracing_degraded_while_exports_fail() -> None:
    from tests.api.app import DEV

    export = DEV.settings.observability.export.model_copy(update={"retry_backoff_milliseconds": 5})
    app = make_app([Turn.say("Hi.")], tracing=True, observability={"export": export})
    assert app.deps.tracing is not None
    app.deps.tracing.start()
    app.gateway.fail_next("sp_obs_ingest", ZonicError("DATA_UNAVAILABLE"), times=1000)
    try:
        async with app.client() as client:
            await client.post("/v1/runs", json=start("sync"), headers=auth())
            await app.deps.tracing.flush()
            ready = await client.get("/readyz")
    finally:
        await app.deps.tracing.aclose()
    assert ready.status_code == 200
    assert ready.json()["status"] == "degraded" and ready.json()["checks"]["tracing"] == "degraded"


# --- H-12 ---------------------------------------------------------------------------------------


async def test_H12_cancelling_a_waiting_run_cancels_it_and_its_interrupts() -> None:
    app = make_app([Turn.say(DRAFT)], workflows={FLOW: APPROVAL})
    async with app.client() as client:
        run_id, interrupt_id, _ = await paused(app, client)
        cancelled = await client.post(f"/v1/runs/{run_id}/cancel", headers=auth())
        late = await client.post(
            f"/v1/runs/{run_id}/interrupts/{interrupt_id}",
            json={"answer": {"approved": True}},
            headers=auth(),
        )
        listed = (await client.get(f"/v1/runs/{run_id}/interrupts", headers=auth())).json()["items"]
        foreign = await client.post(f"/v1/runs/{run_id}/cancel", headers=auth("mallory"))
    assert cancelled.status_code == 202 and cancelled.json()["status"] == "cancelled"
    assert [i["status"] for i in app.schema.t("runtime_interrupt")] == ["cancelled"]
    assert app.schema.t("runtime_checkpoint") == [] and listed == []
    assert late.status_code in (404, 410) and foreign.status_code == 404


# --- E-17 ---------------------------------------------------------------------------------------


async def test_E17_cancel_through_the_api_during_a_model_stream_ends_the_run_cancelled() -> None:
    app = make_app([Turn.say("A very long answer that never finishes.", chunk_delay=0.2)])
    async with app.client() as client:
        streaming = asyncio.create_task(client.post("/v1/runs", json=start("stream"), headers=auth()))
        for _ in range(200):
            if app.models.model.requests and app.deps.registry.runs():
                break
            await asyncio.sleep(0.01)
        [active] = app.deps.registry.runs()
        cancelled = await client.post(f"/v1/runs/{active.run_id}/cancel", headers=auth())
        events = sse_events((await asyncio.wait_for(streaming, 5)).text)
        again = await client.post(f"/v1/runs/{active.run_id}/cancel", headers=auth())
    assert cancelled.status_code == 202 and cancelled.json()["status"] == "cancelled"
    assert events[-1]["type"] == "run.cancelled" and events[-1]["data"]["reason"] == "cancelled"
    assert again.status_code == 202 and again.json()["status"] == "cancelled"  # finished: as it is
