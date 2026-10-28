"""The production tracer through the app (P2-08: L-11): runs export through the fake gateway's
``sp_obs_ingest``, and a gateway that cannot take them leaves runs alone."""

from __future__ import annotations

import uuid
from typing import Any

from tests.api.app import CHAT, DEV, auth, make_app
from tests.fakes.scripted_chat_model import Turn
from zonic_runtime.errors import ZonicError
from zonic_runtime.models.observability import ObsObservationRecord, ObsTraceRecord

WORKFLOW = str(uuid.uuid4())


def sync(text: str) -> dict[str, Any]:
    return {"agenticWorkflowId": WORKFLOW, "input": {"text": text}, "mode": "sync"}


def fast_export(app_settings: Any) -> Any:
    export = app_settings.observability.export.model_copy(
        update={"retry_attempts": 1, "retry_backoff_milliseconds": 5}
    )
    return {"export": export}


async def test_O01_a_run_through_the_app_is_ingested_with_its_header_tenant_and_workflow() -> None:
    app = make_app(
        [Turn.say("Traced.")],
        workflows={WORKFLOW: CHAT},
        tracing=True,
        observability=fast_export(DEV.settings),
    )
    assert app.deps.tracing is not None and app.obs is not None
    app.deps.tracing.start()
    try:
        async with app.client() as client:
            run = (await client.post("/v1/runs", json=sync("hello"), headers=auth())).json()
        await app.deps.tracing.flush()
    finally:
        await app.deps.tracing.aclose()
    [trace] = app.obs.traces.values()
    ObsTraceRecord.model_validate(trace)
    assert trace["runId"] == run["runId"] and trace["sessionId"] == run["sessionId"]
    assert trace["agenticWorkflowId"] == WORKFLOW and trace["tenantId"] == "t1" and trace["userId"] == "alice"
    assert trace["status"] == "succeeded"
    nodes = {o["nodeId"]: o for o in app.obs.observations.values() if "nodeId" in o and o.get("visit") == 1}
    assert set(nodes) == {"start", "llm", "out"} and nodes["llm"]["type"] == "generation"
    for observation in app.obs.observations.values():
        ObsObservationRecord.model_validate(observation)


async def test_L11_with_the_gateway_unreachable_for_ingest_runs_are_normal_and_readiness_is_degraded() -> (
    None
):
    app = make_app(
        [Turn.say("First."), Turn.say("Second.")],
        workflows={WORKFLOW: CHAT},
        tracing=True,
        observability=fast_export(DEV.settings),
    )
    tracing = app.deps.tracing
    assert tracing is not None and app.obs is not None
    tracing.start()
    app.gateway.fail_next("sp_obs_ingest", ZonicError("DATA_UNAVAILABLE"), times=1000)
    try:
        async with app.client() as client:
            first = (await client.post("/v1/runs", json=sync("one"), headers=auth())).json()
            await tracing.flush()
            degraded = (await client.get("/readyz")).json()
            assert first["status"] == "succeeded" and app.obs.observations == {}
            assert degraded["status"] == "degraded" and degraded["checks"]["tracing"] == "degraded"
            assert tracing.exporter.queued > 0 and tracing.exporter.dropped == 0

            app.gateway._faults.clear()  # the gateway is back
            second = (await client.post("/v1/runs", json=sync("two"), headers=auth())).json()
            await tracing.flush()
            ready = (await client.get("/readyz")).json()
    finally:
        await tracing.aclose()
    assert second["status"] == "succeeded"
    assert {t["runId"] for t in app.obs.traces.values()} == {first["runId"], second["runId"]}
    assert ready["status"] == "ok" and ready["checks"]["tracing"] == "ok" and tracing.exporter.queued == 0
