"""Traces, usage, feedback and maintenance (P3-11: L-14, L-15, O-07, O-08), through the app with the
production tracer exporting to the fake gateway's ``agent_observability``."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from tests.api.app import DEV, FakeApp, auth, make_app
from tests.fakes.scripted_chat_model import Turn
from zonic_runtime.application.caller import Caller
from zonic_runtime.application.observability import MaintenanceService, TraceService, score_id
from zonic_runtime.errors import ZonicError
from zonic_runtime.integrations.gateway.repository import GatewayRepository
from zonic_runtime.integrations.langfuse.export import FanOutExporter
from zonic_runtime.integrations.langfuse.provider import trace_id
from zonic_runtime.integrations.langfuse.tracing import GatewayTracing

SUPPORT = ["runtime-support"]


def support(subject: str = "alice") -> dict[str, str]:
    return auth(subject, groups=SUPPORT)


async def traced_run(text: str = "Hello there") -> tuple[FakeApp, dict[str, Any]]:
    export = DEV.settings.observability.export.model_copy(update={"retry_backoff_milliseconds": 5})
    app = make_app([Turn.say("Hi! How can I help?")], tracing=True, observability={"export": export})
    assert app.deps.tracing is not None
    app.deps.tracing.start()
    try:
        async with app.client() as client:
            body = {"agenticWorkflowId": "wf-chat", "input": {"text": text}, "mode": "sync"}
            run = (await client.post("/v1/runs", json=body, headers=auth())).json()
        await app.deps.tracing.flush()
    finally:
        await app.deps.tracing.aclose()
    assert run["status"] == "succeeded", run
    return app, run


# --- L-15 ---------------------------------------------------------------------------------------


async def test_L15_a_trace_role_reads_the_runs_trace_tree() -> None:
    app, run = await traced_run()
    async with app.client() as client:
        found = await client.get(f"/v1/runs/{run['runId']}/trace", headers=support())
    assert found.status_code == 200, found.text
    trace = found.json()
    assert trace["traceId"] == trace_id(run["runId"]) and trace["runId"] == run["runId"]
    assert trace["status"] == "succeeded" and trace["observationCount"] == len(app.obs.observations)  # type: ignore[union-attr]

    def walk(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [x for item in items for x in [item, *walk(item["children"])]]

    every = walk(trace["observations"])
    assert len(every) == trace["observationCount"] and len(trace["observations"]) < len(every)  # nested
    assert all(
        child["parentObservationId"] == item["observationId"] for item in every for child in item["children"]
    )
    assert {o.get("nodeId") for o in every} >= {"start", "llm", "out"}


async def test_L15_without_a_trace_role_it_is_403_and_another_users_run_is_404() -> None:
    app, run = await traced_run()
    async with app.client() as client:
        plain = await client.get(f"/v1/runs/{run['runId']}/trace", headers=auth())
        other = await client.get(f"/v1/runs/{run['runId']}/trace", headers=support("mallory"))
        usage = await client.get("/v1/observability/usage", headers=auth())
    assert plain.status_code == 403 and plain.json()["error"]["code"] == "FORBIDDEN"
    assert other.status_code == 404 and usage.status_code == 403


async def test_usage_by_node_and_by_model_for_the_callers_tenant() -> None:
    app, _ = await traced_run()
    async with app.client() as client:
        nodes = (await client.get("/v1/observability/usage", headers=support())).json()
        models = (
            await client.get("/v1/observability/usage", params={"groupBy": "model"}, headers=support())
        ).json()
        bad = await client.get(
            "/v1/observability/usage", params={"from": "2026-12-01", "to": "2026-01-01"}, headers=support()
        )
    assert nodes["groupBy"] == "node" and {i["nodeType"] for i in nodes["items"]} >= {"start", "llm"}
    assert all(i["executions"] >= 1 for i in nodes["items"])
    assert models["groupBy"] == "model" and models["items"] and all(i["calls"] >= 1 for i in models["items"])
    assert bad.status_code == 400


async def test_the_trace_link_needs_a_langfuse_server() -> None:
    app, run = await traced_run()
    async with app.client() as client:
        missing = await client.get(f"/v1/runs/{run['runId']}/trace-link", headers=support())
    assert missing.status_code == 404
    traces = TraceService(
        GatewayRepository(app.gateway), app.gateway, DEV.settings, langfuse_url="https://lf.example.test/"
    )
    link = await traces.link(Caller(subject="alice", groups=frozenset(SUPPORT)), run["runId"])
    assert link.url == f"https://lf.example.test/trace/{trace_id(run['runId'])}"


# --- L-14 ---------------------------------------------------------------------------------------


async def test_L14_feedback_is_stored_and_scored_on_the_trace_and_a_new_rating_replaces_it() -> None:
    app, run = await traced_run()
    async with app.client() as client:
        messages = (await client.get(f"/v1/sessions/{run['sessionId']}/messages", headers=auth())).json()[
            "items"
        ]
        answer = next(m for m in messages if m["role"] == "assistant")
        path = f"/v1/messages/{answer['messageId']}/feedback"
        first = await client.post(path, json={"rating": 1, "comment": "useful"}, headers=auth())
        second = await client.post(path, json={"rating": -1}, headers=auth())
        foreign = await client.post(path, json={"rating": 1}, headers=auth("mallory"))
        again = (await client.get(f"/v1/sessions/{run['sessionId']}/messages", headers=auth())).json()[
            "items"
        ]
    assert first.status_code == 204 and second.status_code == 204
    assert foreign.status_code == 404
    assert next(m for m in again if m["messageId"] == answer["messageId"])["feedbackRating"] == -1
    [score] = [
        s for s in app.obs.scores.values() if s["name"] == DEV.settings.observability.feedback_score_name
    ]  # type: ignore[union-attr]
    assert score["scoreId"] == score_id(answer["messageId"], "alice") and score["value"] == -1
    assert score["traceId"] == trace_id(run["runId"]) and score["dataType"] == "NUMERIC"


async def test_L14_a_run_without_a_trace_keeps_its_rating() -> None:
    app = make_app([Turn.say("Hi!")])
    async with app.client() as client:
        body = {"agenticWorkflowId": "wf-chat", "input": {"text": "Hello"}, "mode": "sync"}
        run = (await client.post("/v1/runs", json=body, headers=auth())).json()
        messages = (await client.get(f"/v1/sessions/{run['sessionId']}/messages", headers=auth())).json()[
            "items"
        ]
        answer = next(m for m in messages if m["role"] == "assistant")
        saved = await client.post(
            f"/v1/messages/{answer['messageId']}/feedback", json={"rating": 5}, headers=auth()
        )
    assert saved.status_code == 204


# --- O-07 ---------------------------------------------------------------------------------------


async def test_O07_a_maintenance_pass_runs_every_step_and_a_failing_one_does_not_stop_the_others() -> None:
    app, _ = await traced_run()
    session = app.schema.t("runtime_session")[0]
    session["last_message_at"] = "2020-01-01T00:00:00+00:00"
    service = MaintenanceService(GatewayRepository(app.gateway), DEV.settings, app.clock)
    report = await service.run()
    assert report["steps"] == {
        "partitions": "ok",
        "traceRetention": "ok",
        "rollups": "ok",
        "runtimeRetention": "ok",
    }
    assert [name for name, _ in app.obs.maintenance] == ["partitions", "traceRetention", "rollups"]  # type: ignore[union-attr]
    assert app.obs.maintenance[0][1][0] == DEV.settings.observability.partition_months_ahead  # type: ignore[union-attr]
    assert session["status"] == "archived"
    app.gateway.fail_next("sp_obs_partition_ensure", ZonicError("DATA_UNAVAILABLE"))
    report = await service.run()
    assert report["steps"]["partitions"] == "DATA_UNAVAILABLE" and report["steps"]["rollups"] == "ok"


# --- O-08 ---------------------------------------------------------------------------------------


class Recording(SpanExporter):
    def __init__(self, fail: bool = False) -> None:
        self.spans: list[ReadableSpan] = []
        self.fail = fail

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        if self.fail:
            raise ConnectionError("langfuse is down")
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        if self.fail:
            raise ConnectionError("langfuse is down")


def test_O08_the_same_spans_reach_both_and_the_gateway_is_unaffected_by_langfuse_failures() -> None:
    from tests.unit.test_gateway_exporter import span

    gateway, langfuse = Recording(), Recording()
    both = FanOutExporter(gateway, langfuse)
    batch = [span(1), span(2)]
    assert both.export(batch) == SpanExportResult.SUCCESS
    assert gateway.spans == langfuse.spans == batch
    langfuse.fail = True
    assert both.export([span(3)]) == SpanExportResult.SUCCESS and len(gateway.spans) == 3
    both.shutdown()
    assert both.secondary_failures == 2


def test_O08_the_langfuse_exporter_runs_beside_the_gateway_and_needs_its_keys() -> None:
    from dataclasses import replace

    from tests.fakes.in_memory_gateway import InMemoryGateway

    observability = DEV.settings.observability.model_copy(update={"exporters": ["gateway", "langfuse"]})
    resources = replace(DEV, settings=DEV.settings.model_copy(update={"observability": observability}))
    tracing = GatewayTracing.create(resources, InMemoryGateway(), langfuse=Recording())
    assert tracing is not None and isinstance(tracing.tracer.exporter.inner, FanOutExporter)
    with pytest.raises(ZonicError, match="RESOURCE_INVALID"):
        GatewayTracing.create(resources, InMemoryGateway())
