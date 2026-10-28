"""Durable ``async`` runs: claims, leases, fencing, drain and handover (P4-05: Q-10 to Q-15, R-03,
P-10, P-14), with two instances on one fake gateway.

``CHAIN`` is Start -> a -> b -> c -> Out, three LLM nodes in a row, so a run can stop between
supersteps on one instance and go on, or start over, on the other. A "lost" instance is simulated by
stopping its run without saving (as when its process dies) and expiring the run's lease.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from tests.api.app import FakeApp, auth, make_app, sse_events
from tests.fakes.scripted_chat_model import Turn
from zonic_runtime.api.internal import create_internal_app
from zonic_runtime.errors import ZonicError
from zonic_runtime.integrations.gateway.repository import GatewayRepository
from zonic_runtime.models.gateway.runtime import (
    CheckpointSaveArgs,
    DatasetSaveArgs,
    InterruptCreateArgs,
    RunFinishArgs,
)

FLOW = "wf-chain"


def llm(node_id: str) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "llm",
        "name": node_id,
        "config": {"messages": [{"role": "user", "content": f"{node_id}: {{{{system.userQuery}}}}"}]},
    }


CHAIN = {
    "schemaVersion": "2.0",
    "nodes": [
        {"id": "start", "type": "start", "name": "Start", "config": {}},
        llm("a"),
        llm("b"),
        llm("c"),
        {
            "id": "out",
            "type": "output",
            "name": "Out",
            "config": {"outputMapping": [{"target": "answer", "source": "{{nodes.c.output.text}}"}]},
        },
    ],
    "edges": [{"source": s, "target": t} for s, t in [("start", "a"), ("a", "b"), ("b", "c"), ("c", "out")]],
}


def durable(script: list[Turn], **settings: Any) -> FakeApp:
    return make_app(script, workflows={FLOW: CHAIN}, durable=True, **settings)


def body(mode: str = "async", text: str = "go") -> dict[str, Any]:
    return {"agenticWorkflowId": FLOW, "input": {"text": text}, "mode": mode}


async def until(check: Any, seconds: float = 5) -> None:
    for _ in range(int(seconds / 0.01)):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting")


async def finished(app: FakeApp, run_id: str) -> dict[str, Any]:
    """The run's row once it is finished (its task may end before anyone waits on it)."""
    await until(lambda: (app.schema.run(run_id) or {}).get("status") in ("succeeded", "failed", "cancelled"))
    row = app.schema.run(run_id)
    assert row is not None
    return row


def die(app: FakeApp, run_id: str) -> None:
    """The instance's process stops: the run is not saved, and its lease runs out."""
    active = app.deps.registry.get(run_id)
    assert active is not None
    active.stopped = True
    active.cancel("SHUTDOWN")
    app.schema.run(run_id)["lease_expires_at"] = "2020-01-01T00:00:00+00:00"  # type: ignore[index]


def requests_of(app: FakeApp) -> list[str]:
    """The node each model call was for (its prompt starts with the node ID)."""
    return [str(r.messages[-1].content).split(":")[0] for r in app.models.model.requests]


# --- Q-10 ---------------------------------------------------------------------------------------


async def test_Q10_an_async_run_with_a_free_permit_starts_here_with_this_instances_lease() -> None:
    app = durable([Turn.say("A"), Turn.say("B"), Turn.say("C")])
    async with app.client() as client:
        accepted = (await client.post("/v1/runs", json=body(), headers=auth())).json()
        row = app.schema.run(accepted["runId"])
        assert row is not None and row["status"] == "running"
        assert row["lease_owner"] == app.deps.runs.durable.instance.id  # type: ignore[union-attr]
        await app.deps.registry.wait(accepted["runId"], 5)
    assert row["status"] == "succeeded" and row["lease_owner"] is None


async def test_Q10_without_a_free_permit_an_async_run_is_queued_and_claimed_elsewhere() -> None:
    a = durable([])
    b = a.twin([Turn.say("A"), Turn.say("B"), Turn.say("C")])

    def full() -> Any:
        raise ZonicError("RATE_LIMITED", retry_after=1)

    a.deps.runs._executor.admit = full  # type: ignore[method-assign]
    async with a.client() as client:
        accepted = await client.post("/v1/runs", json=body(), headers=auth())
    assert accepted.status_code == 202 and accepted.json()["status"] == "queued"
    run_id = accepted.json()["runId"]
    assert b.deps.claimer is not None
    [claimed] = await b.deps.claimer.claim()  # one claim interval later
    assert claimed.run_id == run_id and claimed.previous_status == "queued" and claimed.attempt == 1
    row = await finished(b, run_id)
    assert row["status"] == "succeeded" and row["result"] == {"answer": "C"}
    assert requests_of(b) == ["a", "b", "c"]


# --- Q-11 ---------------------------------------------------------------------------------------


async def test_Q11_claims_go_round_robin_across_tenants() -> None:
    a = durable([], async_runs={"claim_batch": 2})
    b = a.twin([Turn.say("x")] * 12)

    def full() -> Any:
        raise ZonicError("RATE_LIMITED", retry_after=1)

    a.deps.runs._executor.admit = full  # type: ignore[method-assign]
    async with a.client() as client:
        for user in range(5):  # tenant A queues five runs, then tenant B one
            await client.post("/v1/runs", json=body(), headers=auth(f"a-{user}", tenant="tenant-a"))
        lone = (await client.post("/v1/runs", json=body(), headers=auth("b-0", tenant="tenant-b"))).json()
    assert b.deps.claimer is not None
    claimed = await b.deps.claimer.claim()
    tenants = {a.schema.run(c.run_id)["tenant_id"] for c in claimed}  # type: ignore[index]
    assert len(claimed) == 2 and tenants == {"tenant-a", "tenant-b"}
    assert lone["runId"] in {c.run_id for c in claimed}  # in the first cycle, not after the five


# --- Q-12, Q-13 -----------------------------------------------------------------------------------


async def test_Q12_a_run_lost_mid_turn_without_write_behind_runs_its_turn_again_elsewhere() -> None:
    a = durable([Turn.say("A"), Turn.say("never", delay=30)])
    b = a.twin([Turn.say("A2"), Turn.say("B2"), Turn.say("C2")])
    async with a.client() as client:
        run_id = (await client.post("/v1/runs", json=body(), headers=auth())).json()["runId"]
        await until(lambda: len(a.models.model.requests) == 2)
        die(a, run_id)
        await a.deps.registry.wait(run_id, 5)
    assert b.deps.claimer is not None
    [claimed] = await b.deps.claimer.claim()
    assert claimed.previous_status == "running" and claimed.attempt == 2
    row = await finished(b, run_id)
    assert row["status"] == "succeeded" and row["result"] == {"answer": "C2"}
    assert requests_of(b) == ["a", "b", "c"]  # the turn ran again from the session's saved state


async def test_Q12_with_write_behind_a_lost_run_resumes_from_its_last_checkpoint() -> None:
    settings = {"state": {"async_checkpoints": True, "checkpoint_interval_seconds": 0.05}}
    a = durable([Turn.say("A"), Turn.say("never", delay=30)], **settings)
    b = a.twin([Turn.say("B2"), Turn.say("C2")])
    async with a.client() as client:
        run_id = (await client.post("/v1/runs", json=body(), headers=auth())).json()["runId"]
        await until(lambda: len(a.models.model.requests) == 2 and bool(a.schema.t("runtime_checkpoint")))
        die(a, run_id)
        await a.deps.registry.wait(run_id, 5)
    assert b.deps.claimer is not None
    await b.deps.claimer.claim()
    row = await finished(b, run_id)
    assert row["status"] == "succeeded" and row["result"] == {"answer": "C2"}
    assert requests_of(b) == ["b", "c"]  # node a's superstep was not repeated


async def test_Q13_after_a_takeover_the_old_owner_is_fenced_off_and_told_it_lost_the_run() -> None:
    a = durable([Turn.say("A"), Turn.say("slow", delay=30)])
    b = a.twin([Turn.say("A2"), Turn.say("B2"), Turn.say("C2")])
    async with a.client() as client:
        run_id = (await client.post("/v1/runs", json=body(), headers=auth())).json()["runId"]
        await until(lambda: len(a.models.model.requests) == 2)
        a.schema.run(run_id)["lease_expires_at"] = "2020-01-01T00:00:00+00:00"  # type: ignore[index]
        assert b.deps.claimer is not None
        await b.deps.claimer.claim()  # attempt 2 belongs to b now
        repository = GatewayRepository(a.gateway)
        with pytest.raises(ZonicError) as caught:
            await repository.call(RunFinishArgs(run_id=run_id, attempt=1, save={"status": "succeeded"}))
        assert caught.value.code == "RUN_LEASE_LOST"
        durable_a = a.deps.runs.durable
        assert durable_a is not None
        assert await durable_a.leases.beat() == [run_id]  # a's heartbeat reports the run lost
        await until(lambda: a.deps.registry.get(run_id) is None)  # stopped there, never saved
    row = await finished(b, run_id)
    assert row["status"] == "succeeded" and row["attempt"] == 2


# --- Q-14 ---------------------------------------------------------------------------------------


async def test_Q14_a_subscriber_on_another_instance_gets_the_events_through_the_owner() -> None:
    a = durable([Turn.say("A"), Turn.say("B"), Turn.say("C, said very slowly indeed.", chunk_delay=0.1)])
    b = a.twin()
    owner = create_internal_app(a.deps)
    b.deps.pools.get(
        "run-events", lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=owner), base_url="http://a")
    )
    async with a.client() as client_a, b.client() as client_b:
        run_id = (await client_a.post("/v1/runs", json=body(), headers=auth())).json()["runId"]
        await until(lambda: len(a.models.model.requests) >= 1)
        path = f"/v1/runs/{run_id}/events"
        whole, resumed = await asyncio.gather(  # both while the run is still running on a
            client_b.get(path, headers=auth()),
            client_b.get(path, headers={**auth(), "Last-Event-ID": "3"}),
        )
    events, after = sse_events(whole.text), sse_events(resumed.text)
    assert events[0]["type"] == "run.started" and events[-1]["type"] == "run.completed"
    seqs = [e["seq"] for e in events]
    assert seqs == list(range(1, len(seqs) + 1))  # all of them, once, in order
    assert [e["seq"] for e in after] == seqs[3:]  # after Last-Event-ID, no duplicates
    assert a.deps.registry.get(run_id) is None and b.models.model.requests == []  # a drove it


# --- Q-15 ---------------------------------------------------------------------------------------


async def test_Q15_an_overloaded_instance_queues_an_async_run_instead_of_refusing_it() -> None:
    app = durable([])
    app.deps.pressure.overloaded = lambda: True  # type: ignore[method-assign]
    async with app.client() as client:
        refused = await client.post("/v1/runs", json=body("sync"), headers=auth())
        queued = await client.post("/v1/runs", json=body("async"), headers=auth("someone-else"))
        ready = (await client.get("/readyz")).json()
    assert refused.status_code == 503 and refused.json()["error"]["code"] == "OVERLOADED"
    assert queued.status_code == 202 and queued.json()["status"] == "queued"
    assert ready["status"] == "degraded"


# --- R-03 ---------------------------------------------------------------------------------------


async def test_R03_a_drained_async_run_resumes_elsewhere_without_repeating_its_supersteps() -> None:
    a = durable([Turn.say("A"), Turn.say("B", delay=0.3)])
    b = a.twin([Turn.say("C")])
    async with a.client() as client:
        run_id = (await client.post("/v1/runs", json=body(), headers=auth())).json()["runId"]
        await until(lambda: len(a.models.model.requests) == 2)  # b is running
        await a.deps.runs.drain(5)  # b finishes; the run stops before c
    row = a.schema.run(run_id)
    assert row is not None and row["status"] == "queued" and row["lease_owner"] is None
    [checkpoint] = a.schema.t("runtime_checkpoint")
    assert checkpoint["kind"] == "async" and checkpoint["attempt"] == 1
    assert b.deps.claimer is not None
    [claimed] = await b.deps.claimer.claim()
    assert claimed.previous_status == "queued" and claimed.attempt == 2
    row = await finished(b, run_id)
    assert row["status"] == "succeeded" and row["result"] == {"answer": "C"}
    assert requests_of(a) == ["a", "b"] and requests_of(b) == ["c"]  # nothing ran twice
    assert a.schema.t("runtime_checkpoint") == []


async def test_R03_a_drain_lets_stream_and_sync_runs_finish_or_fails_them_with_shutdown() -> None:
    app = durable([Turn.say("A"), Turn.say("B"), Turn.say("never", delay=30)])
    async with app.client() as client:
        slow = asyncio.create_task(client.post("/v1/runs", json=body("sync"), headers=auth()))
        await until(lambda: len(app.models.model.requests) == 3)
        await app.deps.runs.drain(0.2)
        answer = (await asyncio.wait_for(slow, 5)).json()
    assert answer["status"] == "failed" and answer["error"]["code"] == "SHUTDOWN"


# --- P-10, P-14 ---------------------------------------------------------------------------------


async def test_P10_writes_of_an_older_attempt_are_rejected() -> None:
    app = durable([Turn.say("A"), Turn.say("B"), Turn.say("C")])
    async with app.client() as client:
        run_id = (await client.post("/v1/runs", json=body(), headers=auth())).json()["runId"]
        await app.deps.registry.wait(run_id, 5)
    row = app.schema.run(run_id)
    assert row is not None
    row.update(status="running", attempt=2)  # a newer attempt took over
    repository = GatewayRepository(app.gateway)
    writes = [
        RunFinishArgs(run_id=run_id, attempt=1, save={"status": "succeeded"}),
        InterruptCreateArgs(
            interrupt={"runId": run_id, "interrupts": [{"nodeId": "a"}]},
            checkpoint={},
            messages=[],
            attempt=1,
        ),
        CheckpointSaveArgs(run_id=run_id, attempt=1, body={}),
        DatasetSaveArgs(run_id=run_id, attempt=1, chunks=[]),
    ]
    for write in writes:
        with pytest.raises(ZonicError) as caught:
            await repository.call(write)
        assert caught.value.code == "RUN_LEASE_LOST", type(write).__name__


async def test_P14_write_behind_saves_at_most_once_per_interval_and_the_latest_wins() -> None:
    settings = {"state": {"async_checkpoints": True, "checkpoint_interval_seconds": 0.1}}
    script = [Turn.say("A"), Turn.say("B", delay=0.35), Turn.say("C", delay=0.35)]
    app = durable(script, **settings)
    async with app.client() as client:
        run_id = (await client.post("/v1/runs", json=body(), headers=auth())).json()["runId"]
        await until(lambda: len(app.models.model.requests) == 3)
        saves = [c for c in app.gateway.calls if c.name == "sp_checkpoint_save"]
        [checkpoint] = app.schema.t("runtime_checkpoint")
        nodes = {c[0] for c in checkpoint["body"]["checkpoints"]}
        outcome = await app.deps.registry.wait(run_id, 5)
    assert outcome is not None and outcome.status == "succeeded"
    assert 1 <= len(saves) <= 8  # ~0.7 s of turn at 0.1 s: never a save per superstep and namespace
    assert "" in nodes  # the root's latest checkpoint is the row's (latest wins)
    assert app.schema.t("runtime_checkpoint") == []  # sp_run_finish deleted it


# --- completion callbacks ------------------------------------------------------------------------


async def test_a_finished_async_run_posts_its_signed_completion_callback() -> None:
    import hashlib
    import hmac
    import json

    from tests.api.app import DEV
    from tests.fakes.egress import FakeEgress
    from zonic_runtime.engine.node_template import ServicePorts
    from zonic_runtime.ports.egress import EgressResponse

    class Secrets:
        def get(self, name: str) -> str | None:
            return "hook-key" if name == "callbacks" else None

    egress = FakeEgress(DEV.egress, answer=lambda _: EgressResponse(status=204))
    sent = egress.calls
    app = make_app(
        [Turn.say("A"), Turn.say("B"), Turn.say("C")],
        workflows={FLOW: CHAIN},
        durable=True,
        ports=ServicePorts(egress=egress, secrets=Secrets()),
    )
    callback = {"url": "https://httpbin.org/post", "secretRef": "secret:callbacks"}
    async with app.client() as client:
        start = {**body(), "options": {"callback": callback}}
        run_id = (await client.post("/v1/runs", json=start, headers=auth())).json()["runId"]
        await app.deps.registry.wait(run_id, 5)
        await until(lambda: bool(sent))
    [request] = sent
    payload = json.loads(request.body)
    assert (
        payload["runId"] == run_id
        and payload["status"] == "succeeded"
        and payload["result"] == {"answer": "C"}
    )
    expected = "sha256=" + hmac.new(b"hook-key", request.body.encode(), hashlib.sha256).hexdigest()
    assert dict(request.headers)["X-Zonic-Signature"] == expected
