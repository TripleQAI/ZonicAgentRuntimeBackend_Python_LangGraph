"""Phase 2 end to end on the deployed gateway (opt-in: ``uv run pytest -m live``).

The app with the real ``HttpGateway`` and the production tracer (``sp_obs_ingest``); only the
model is scripted and the token comes from the test issuer. It runs the live catalog's
``test-agent-workflow`` (the one workflow whose node kinds Phase 2 builds) and checks what the
product database then holds. Every row it writes belongs to owner ``live-langgraph-verify``,
tenant ``live-test``.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.api.app import auth, fake_verifier, sse_events
from tests.fakes.scripted_chat_model import ScriptedChatModel, Turn
from tests.kinds.agents import ScriptedModels
from tests.live.conftest import live_environment
from tests.workflow_fixtures import read_json
from zonic_runtime.api.deps import AppDeps
from zonic_runtime.config.settings import RuntimeResources, load_resources
from zonic_runtime.engine.agents.agent_factory import AgentServices
from zonic_runtime.errors import ZonicError
from zonic_runtime.integrations.gateway.http_gateway import HttpGateway
from zonic_runtime.integrations.langfuse.provider import trace_id
from zonic_runtime.integrations.langfuse.tracing import GatewayTracing
from zonic_runtime.main import create_app
from zonic_runtime.models.gateway.wire import Filter
from zonic_runtime.workflow.loader.loader import WorkflowCaller

pytestmark = pytest.mark.live

ENV = live_environment()
OWNER, TENANT = "live-langgraph-verify", "live-test"
#: The AWS deployment's model stand-ins (Claude codes served by OpenAI until Bedrock is available).
STANDINS = read_json(
    Path(__file__).resolve().parents[2] / "deploy" / "aws" / "resources" / "models.dev.json"
)["substitutes"]
WORKFLOW = "test-agent-workflow"


def resources() -> RuntimeResources:
    """The scripted model answers for every catalog model (``gpt-5-mini`` is not enabled in dev)."""
    found = load_resources(ENV)
    models = {c: e.model_copy(update={"enabled": True}) for c, e in found.models.models.items()}
    return replace(found, models=found.models.model_copy(update={"models": models}))


@dataclass
class LiveApp:
    deps: AppDeps
    gateway: HttpGateway
    client: httpx.AsyncClient
    workflow_id: str

    async def start(self, text: str, mode: str = "sync", **extra: Any) -> httpx.Response:
        body = {"agenticWorkflowId": self.workflow_id, "input": {"text": text}, "mode": mode, **extra}
        return await self.client.post("/v1/runs", json=body, headers=auth(OWNER, tenant=TENANT))

    async def get(self, path: str) -> httpx.Response:
        return await self.client.get(path, headers=auth(OWNER, tenant=TENANT))


@contextlib.asynccontextmanager
async def open_live(
    script: list[Any],
    workflow_name: str = WORKFLOW,
    *,
    ports: Any = None,
    aliases: dict[str, str] | None = None,
    sections: dict[str, dict[str, Any]] | None = None,
    durable: bool = False,
    agents: Callable[[RuntimeResources], AgentServices] | None = None,
    substitutes: bool = False,
    **features: bool,
) -> AsyncIterator[LiveApp]:
    """``ports``: local stand-ins for the services this machine has no access to (the sandbox,
    the MCP server); the gateway and the database are always the deployed ones. ``aliases``: model
    IDs a live document names that the catalog lacks, mapped to a catalog code (the scripted model
    answers for all of them). ``agents``: real model services instead of ``script`` (P5-03);
    ``substitutes``: the AWS deployment's model stand-ins (``deploy/aws/resources/models.dev.json``)."""
    found = resources()
    for name, update in (sections or {}).items():  # settings sections changed for the test
        section = getattr(found.settings, name).model_copy(update=update)
        found = replace(found, settings=found.settings.model_copy(update={name: section}))
    if aliases:
        models = found.models.model_copy(update={"aliases": {**found.models.aliases, **aliases}})
        found = replace(found, models=models)
    if substitutes:
        found = replace(found, models=found.models.model_copy(update={"substitutes": STANDINS}))
    if features:
        settings = found.settings
        found = replace(
            found,
            settings=settings.model_copy(update={"features": settings.features.model_copy(update=features)}),
        )
    gateway = HttpGateway(found.settings.gateway, ENV)
    if not await gateway.ready():
        await gateway.aclose()
        pytest.skip(f"no gateway ready at {ENV.gateway_base_url}")
    services = (
        agents(found)
        if agents is not None
        else AgentServices.from_resources(
            found, builders=ScriptedModels(ScriptedChatModel(script=script)).builders()
        )
    )
    deps = AppDeps.build(
        found,
        gateway,
        fake_verifier(found),
        agents=services,
        tracing=GatewayTracing.create(found, gateway),
        ports=ports(found) if callable(ports) else ports,
        durable=durable,
    )
    deps.start()
    rows = await deps.runtime.loader.catalog(WorkflowCaller(OWNER))
    workflow = next((r.agentic_workflow_id for r in rows if r.name == workflow_name), None)
    if workflow is None:
        await deps.aclose()
        pytest.skip(f"the live catalog has no {workflow_name}")
    transport = httpx.ASGITransport(app=create_app(deps))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://runtime.live") as client:
            yield LiveApp(deps=deps, gateway=gateway, client=client, workflow_id=workflow)
    finally:
        await deps.aclose()


@pytest.fixture
async def live() -> AsyncIterator[LiveApp]:
    async with open_live([lambda request: Turn.say(f"Live answer to: {request.last_text}")] * 10) as app:
        yield app


async def test_live_a_new_session_turn_is_saved_and_read_back(live: LiveApp) -> None:
    response = await live.start("live hello")
    run = response.json()
    assert response.status_code == 200 and run["status"] == "succeeded", run
    assert run["result"] == {"text": "Live answer to: live hello"}
    stored = (await live.get(f"/v1/runs/{run['runId']}")).json()
    assert stored["status"] == "succeeded" and stored["sessionId"] == run["sessionId"]
    nodes = (await live.get(f"/v1/runs/{run['runId']}/nodes")).json()["items"]
    assert len(nodes) == 3 and all(n["status"] == "succeeded" for n in nodes)
    messages = (await live.get(f"/v1/sessions/{run['sessionId']}/messages")).json()["items"]
    assert [m["role"] for m in messages] == ["assistant", "user"]  # newest first


async def test_live_a_streamed_turn_ends_with_run_completed(live: LiveApp) -> None:
    response = await live.start("live stream", mode="stream")
    events = sse_events(response.text)
    assert response.status_code == 200 and events[0]["type"] == "run.started"
    assert events[-1]["type"] == "run.completed" and [e["seq"] for e in events] == list(
        range(1, len(events) + 1)
    )


async def test_live_the_runs_trace_is_ingested_into_agent_observability(live: LiveApp) -> None:
    run = (await live.start("live traced")).json()
    tracing = live.deps.tracing
    assert tracing is not None
    await tracing.flush()
    assert tracing.healthy and tracing.exporter.queued == 0, tracing.exporter.last_error
    by_trace = [Filter(column="trace_id", value=trace_id(run["runId"]))]
    [trace] = (
        await live.gateway.view("observability", "observability", "v_obs_trace", filters=by_trace)
    ).rows
    assert trace["run_id"] == run["runId"] and trace["session_id"] == run["sessionId"]
    assert trace["user_id"] == OWNER and trace["tenant_id"] == TENANT and trace["status"] == "succeeded"
    observations = (
        await live.gateway.view("observability", "observability", "v_obs_observation", filters=by_trace)
    ).rows
    timeline = (await live.get(f"/v1/runs/{run['runId']}/nodes")).json()["items"]
    visited = {o["node_id"] for o in observations if o.get("visit") == 1}
    assert visited == {n["nodeId"] for n in timeline}  # one node observation per visit
    assert any(o["type"] == "generation" for o in observations)


async def test_live_a_second_turn_on_the_session_reads_its_state_and_history(live: LiveApp) -> None:
    """The warm turn's start is one sequential batch (P2-04, P-05); the gateway admits it for
    ``zonic-agent-runtime`` with each item checked against the caller's datasources."""
    first = (await live.start("first turn")).json()
    second = await live.start("second turn", sessionId=first["sessionId"])
    run = second.json()
    assert second.status_code == 200 and run["status"] == "succeeded", second.text
    assert run["sessionId"] == first["sessionId"] and run["result"] == {"text": "Live answer to: second turn"}
    messages = (await live.get(f"/v1/sessions/{first['sessionId']}/messages")).json()["items"]
    assert [m["role"] for m in messages] == ["assistant", "user", "assistant", "user"]  # newest first


async def test_live_a_turn_keeps_an_http_response_as_a_dataset_and_pages_it(live_http: LiveApp) -> None:
    """P3-04, P3-05 on the deployed gateway: the open-data fsh-facility-agent's HTTP branch calls
    NYC Open Data through the egress guard; the response is a ``database``-store dataset (no cache
    service here) whose chunks and metadata the turn-end transactional batch saves, and
    ``GET /v1/datasets/{id}`` pages it back through ``v_dataset`` for the owner only."""
    response = await live_http.start("Which facilities cost the most?")
    run = response.json()
    assert response.status_code == 200 and run["status"] == "succeeded", run
    nodes = (await live_http.get(f"/v1/runs/{run['runId']}/nodes")).json()["items"]
    http = next(n for n in nodes if n["nodeId"].startswith("http_request"))
    assert http["status"] == "succeeded"
    rows = await _dataset_rows(live_http, run["runId"])
    assert rows, "no dataset metadata saved"
    [meta] = [r for r in rows if r["chunk_no"] == 0]
    assert meta["kind"] == "http_response" and meta["store"] == "database" and meta["size_bytes"] > 0
    page = await live_http.get(f"/v1/datasets/{meta['dataset_id']}?pageSize=5")
    assert page.status_code == 200, page.text
    assert page.json()["total"] == 10 and len(page.json()["rows"]) == 5
    other = await live_http.client.get(
        f"/v1/datasets/{meta['dataset_id']}", headers=auth("someone-else", tenant=TENANT)
    )
    assert other.status_code == 404


async def _dataset_rows(app: LiveApp, run_id: str) -> list[dict[str, Any]]:
    from zonic_runtime.integrations.gateway.repository import GatewayRepository
    from zonic_runtime.models.gateway.runtime import DatasetRow

    found = await GatewayRepository(app.gateway).read(
        DatasetRow, owner=OWNER, filters=[Filter(column="run_id", value=run_id)]
    )
    return [r.model_dump(mode="json") for r in found]


@pytest.fixture
async def live_http() -> AsyncIterator[LiveApp]:
    script = [
        Turn.call("handoff_to", {"target": "sql-query-generator", "reason": "needs a query"}),
        Turn.say("SELECT * FROM sample_open_data.facility LIMIT 10"),
    ]
    async with open_live(script, "fsh-facility-agent") as app:
        yield app


async def test_live_the_trace_endpoint_feedback_and_usage_read_agent_observability(live: LiveApp) -> None:
    """P3-11 on the deployed gateway: a support role reads the run's trace tree; feedback is
    stored and scored on that trace; usage reads the daily rollups."""
    run = (await live.start("live feedback")).json()
    assert live.deps.tracing is not None
    await live.deps.tracing.flush()
    role = auth(OWNER, tenant=TENANT, groups=live.deps.resources.settings.observability.trace_roles[:1])
    trace = await live.client.get(f"/v1/runs/{run['runId']}/trace", headers=role)
    assert trace.status_code == 200, trace.text
    assert trace.json()["runId"] == run["runId"] and trace.json()["observations"]
    assert (await live.get(f"/v1/runs/{run['runId']}/trace")).status_code == 403  # no trace role
    messages = (await live.get(f"/v1/sessions/{run['sessionId']}/messages")).json()["items"]
    answer = next(m for m in messages if m["role"] == "assistant")
    saved = await live.client.post(
        f"/v1/messages/{answer['messageId']}/feedback", json={"rating": 1, "comment": "live"}, headers=role
    )
    assert saved.status_code == 204, saved.text
    scores = (await live.client.get(f"/v1/runs/{run['runId']}/trace", headers=role)).json()["scores"]
    name = live.deps.resources.settings.observability.feedback_score_name
    assert [s["value"] for s in scores if s["name"] == name] == [1]
    usage = await live.client.get("/v1/observability/usage", params={"groupBy": "model"}, headers=role)
    assert usage.status_code == 200 and usage.json()["groupBy"] == "model"


async def test_live_maintenance_ensures_partitions_and_refreshes_rollups_that_match_raw_rows(
    live: LiveApp,
) -> None:
    """O-07 on the deployed gateway, the additive steps only (partitions ahead, rollups); the
    retention purges delete product data and are tested offline."""
    run = (await live.start("live rollups")).json()
    assert live.deps.tracing is not None and run["status"] == "succeeded"
    await live.deps.tracing.flush()
    report = await live.deps.maintenance.run(only=lambda step: step in ("partitions", "rollups"))
    assert report["steps"] == {"partitions": "ok", "rollups": "ok"}
    today = live.deps.clock.now().date().isoformat()
    rollups = (
        await live.gateway.view(
            "observability",
            "observability",
            "v_obs_model_usage_daily",
            filters=[Filter(column="day", value=today), Filter(column="tenant_id", value=TENANT)],
        )
    ).rows
    raw = (
        await live.gateway.view(
            "observability",
            "observability",
            "v_obs_observation",
            filters=[Filter(column="tenant_id", value=TENANT), Filter(column="model", op="is_not_null")],
        )
    ).rows
    calls = sum(1 for o in raw if str(o.get("start_time", ""))[:10] == today)
    assert sum(int(r["calls"]) for r in rollups) == calls > 0


def open_facility(script: list[Any], **sections: dict[str, Any]) -> Any:
    """The live catalog's facility-operations (the Facility Operations Swarm). The script node runs
    under the local interpreter and the members' MCP tools are a local fake (no sandbox or MCP
    server is reachable from here); the weather is the real Open-Meteo call."""
    from tests.fakes.mcp_server import FakeMcpServer
    from tests.unit.test_sandbox import FakePiston
    from zonic_runtime.engine.node_template import ServicePorts
    from zonic_runtime.integrations.egress.guard import EgressGuard
    from zonic_runtime.integrations.mcp.execute import McpService

    def ports(found: RuntimeResources) -> Any:
        return ServicePorts(
            egress=EgressGuard(found.egress),
            sandbox=FakePiston().sandbox(),
            mcp=McpService(FakeMcpServer().pool()),
        )

    return open_live(script, "facility-operations", ports=ports, sections=sections, script_node=True)


def facility_turns(*members: Turn) -> list[Turn]:
    import json

    from tests.scenarios.builder.scenarios import ALLOWED, FACILITY_SCOPE

    return [ALLOWED, Turn.say(json.dumps(FACILITY_SCOPE)), *members]


@pytest.fixture
async def live_facility() -> AsyncIterator[LiveApp]:
    """The coordinator hands straight to the report writer, whose SEVERITY 8 sends the report to
    publish_approval."""
    from tests.scenarios.builder.scenarios import transfer

    script = facility_turns(
        transfer("report_writer", "Summarise the costs for Baruch Houses"),
        Turn.say("# Report\nCosts doubled.\nSEVERITY: 8"),
    )
    async with open_facility(script) as app:
        yield app


async def test_V03_a_swarm_run_pauses_for_approval_and_resumes_on_the_deployed_gateway(
    live_facility: LiveApp,
) -> None:
    """P4-01 and P4-02 on the deployed gateway: the Swarm hands off and its report pauses the run at
    publish_approval with one sp_interrupt_create (the interrupt row, the checkpoint row, the run
    waiting); the answer is recorded with sp_interrupt_answer and the run resumes from the
    checkpoint on the next attempt, and sp_run_finish deletes the checkpoint."""
    from zonic_runtime.integrations.gateway.repository import GatewayRepository
    from zonic_runtime.models.gateway.runtime import CheckpointRow

    app = live_facility
    response = await app.start("Why did electricity costs rise at Baruch Houses in Q1 2024?", mode="stream")
    events = sse_events(response.text)
    assert events[-1]["type"] == "run.waiting", events[-1]
    handoffs = [e["data"] for e in events if e["type"] == "swarm.handoff"]
    assert handoffs == [
        {"from": "coordinator", "to": ["report_writer"], "message": "Summarise the costs for Baruch Houses"}
    ]
    run_id = response.headers["x-run-id"]
    [pending] = (await app.get(f"/v1/runs/{run_id}/interrupts")).json()["items"]
    assert pending["nodeId"] == "approval_1" and pending["kind"] == "approval"
    assert pending["prompt"]["text"].startswith("Severity 8/10.")
    repository = GatewayRepository(app.gateway)
    checkpoint = await repository.read_one(
        CheckpointRow, owner=OWNER, filters=[Filter(column="run_id", value=run_id)]
    )
    assert checkpoint is not None and checkpoint.kind == "pause" and checkpoint.attempt == 1
    waiting = (await app.get(f"/v1/runs/{run_id}")).json()
    assert waiting["status"] == "waiting_input" and waiting["pendingInterrupts"] == [pending["interruptId"]]

    body = {"answer": {"approved": True, "comment": "live check"}, "mode": "sync"}
    resumed = await app.client.post(
        f"/v1/runs/{run_id}/interrupts/{pending['interruptId']}",
        json=body,
        headers=auth(OWNER, tenant=TENANT),
    )
    done = resumed.json()
    assert resumed.status_code == 200 and done["status"] == "succeeded", done
    assert done["result"]["status"] == "published" and done["result"]["severity"] == 8
    assert done["result"]["finalAgent"] == "report_writer"
    gone = await repository.read_one(
        CheckpointRow, owner=OWNER, filters=[Filter(column="run_id", value=run_id)]
    )
    assert gone is None
    assert (await app.get(f"/v1/runs/{run_id}/interrupts")).json()["items"] == []
    again = await app.client.post(
        f"/v1/runs/{run_id}/interrupts/{pending['interruptId']}",
        json=body,
        headers=auth(OWNER, tenant=TENANT),
    )
    assert again.status_code == 409 and again.json()["error"]["code"] == "INTERRUPT_ALREADY_ANSWERED"


async def test_live_a_gated_tool_call_pauses_the_swarm_and_runs_once_approved() -> None:
    """P4-03 on the deployed gateway: with ``current_time`` in ``tools.requireApproval``, the
    coordinator's call pauses the run (a ``tool_approval`` interrupt row and checkpoint); approved, the
    same member goes on, hands to the writer, and the run finishes without review (SEVERITY 3)."""
    from tests.scenarios.builder.scenarios import transfer

    script = facility_turns(
        Turn.call("current_time", {}),
        transfer("report_writer", "Summarise"),
        Turn.say("# Report\nSteady.\nSEVERITY: 3"),
    )
    tools = {"require_approval": ["current_time"]}
    async with open_facility(script, tools=tools) as app:
        response = await app.start("What changed at Baruch Houses in Q1 2024?", mode="stream")
        events = sse_events(response.text)
        assert events[-1]["type"] == "run.waiting", events[-1]
        run_id = response.headers["x-run-id"]
        [pending] = (await app.get(f"/v1/runs/{run_id}/interrupts")).json()["items"]
        assert pending["kind"] == "tool_approval" and pending["nodeId"] == "agent_coordinator"
        assert pending["prompt"]["tool"] == "current_time"
        body = {"answer": {"type": "approve"}, "mode": "sync"}
        done = (
            await app.client.post(
                f"/v1/runs/{run_id}/interrupts/{pending['interruptId']}",
                json=body,
                headers=auth(OWNER, tenant=TENANT),
            )
        ).json()
    assert done["status"] == "succeeded", done
    assert done["result"]["status"] == "published" and done["result"]["severity"] == 3


async def test_live_an_async_run_answers_202_and_then_succeeds(live: LiveApp) -> None:
    """P4-04 on the deployed gateway: ``async`` answers at once; the run row then shows the turn."""
    accepted = await live.start("live async", mode="async")
    body = accepted.json()
    assert accepted.status_code == 202 and body["links"]["self"] == f"/v1/runs/{body['runId']}"
    outcome = await live.deps.registry.wait(body["runId"], 30)
    assert outcome is not None and outcome.status == "succeeded"
    run = (await live.get(body["links"]["self"])).json()
    assert run["status"] == "succeeded" and run["mode"] == "async"


async def test_live_cancelling_a_waiting_run_cancels_its_interrupts(live_facility: LiveApp) -> None:
    """P4-04, H-12 on the deployed gateway: ``sp_run_transition`` to ``cancelled`` closes the pause:
    no pending interrupt, no checkpoint."""
    from zonic_runtime.integrations.gateway.repository import GatewayRepository
    from zonic_runtime.models.gateway.runtime import CheckpointRow

    app = live_facility
    response = await app.start("Why did electricity costs rise at Baruch Houses in Q1 2024?", mode="stream")
    assert sse_events(response.text)[-1]["type"] == "run.waiting"
    run_id = response.headers["x-run-id"]
    cancelled = await app.client.post(f"/v1/runs/{run_id}/cancel", headers=auth(OWNER, tenant=TENANT))
    assert cancelled.status_code == 202 and cancelled.json()["status"] == "cancelled"
    assert (await app.get(f"/v1/runs/{run_id}/interrupts")).json()["items"] == []
    repository = GatewayRepository(app.gateway)
    checkpoint = await repository.read_one(
        CheckpointRow, owner=OWNER, filters=[Filter(column="run_id", value=run_id)]
    )
    assert checkpoint is None


async def test_live_a_drained_async_run_is_handed_over_and_the_old_owner_is_fenced_off() -> None:
    """P4-05 (R-03, Q-10, Q-13) on the deployed gateway, with two instances. The first stops claiming
    and drains its async run after the agent's superstep (``sp_checkpoint_save``, ``sp_run_release``).
    Only when that run is the one claimable run in the product database does the second instance
    start: its claimer takes it (``sp_run_claim``) and runs the Output node from the checkpoint.
    The first instance's write with its old attempt is refused, and its heartbeat reports the run
    lost."""
    import asyncio
    import json

    from zonic_runtime.integrations.gateway.repository import GatewayRepository
    from zonic_runtime.models.gateway.runtime import ClaimedRunRow, RunFinishArgs
    from zonic_runtime.models.gateway.wire import FilterOp

    first = {"internal_address": "http://live-a.runtime.test"}
    second = {"internal_address": "http://live-b.runtime.test"}
    async with open_live(
        [Turn.say("Drained after this.", delay=1.0)], sections={"api": first}, durable=True
    ) as a:
        assert a.deps.claimer is not None and a.deps.runs.durable is not None
        await a.deps.claimer.stop()  # a shutdown stops claiming first
        run_id = (await a.start("live drain", mode="async")).json()["runId"]
        await asyncio.sleep(0.5)  # the agent's model call (1 s) is in flight
        await a.deps.runs.drain(10)
        assert (await a.get(f"/v1/runs/{run_id}")).json()["status"] == "queued"
        repository = GatewayRepository(a.gateway)
        claimable = await repository.read(
            ClaimedRunRow, filters=[Filter(column="status", op=FilterOp.IN, value=["queued", "running"])]
        )
        if [str(r.run_id) for r in claimable] != [run_id]:
            pytest.skip("other claimable runs in the product database: not starting a claimer")
        async with open_live([], sections={"api": second}, durable=True) as b:  # claims at start
            done: dict[str, Any] = {}
            for _ in range(200):
                done = (await b.get(f"/v1/runs/{run_id}")).json()
                if done["status"] in ("succeeded", "failed"):
                    break
                await asyncio.sleep(0.05)
            # Two instances share this test's event loop: the second leaves the loop's default
            # executor (which it installed) running for the first, as two processes would not share it.
            b.deps._blocking = None
        with pytest.raises(ZonicError) as caught:  # the first instance's attempt is over
            await repository.call(RunFinishArgs(run_id=run_id, attempt=1, save={"status": "succeeded"}))
        assert caught.value.code == "RUN_LEASE_LOST"
        a.deps.runs.durable.leases.hold(run_id)
        assert await a.deps.runs.durable.leases.beat() == [run_id]
        row = await repository.read_one(ClaimedRunRow, filters=[Filter(column="run_id", value=run_id)])
    assert row is not None and row.attempt == 2 and not row.has_checkpoint  # the claim's attempt finished it
    assert done["status"] == "succeeded", done
    assert "Drained after this." in json.dumps(done["result"])  # the agent's answer, from the checkpoint
