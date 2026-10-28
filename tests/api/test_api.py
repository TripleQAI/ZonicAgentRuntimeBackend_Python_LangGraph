"""The API through HTTP with fakes (P2-03: A-01 to A-07, A-09 to A-12, A-14, A-15, S-01, S-06 to S-08,
S-10, M-06)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import TypeAdapter
from scripts.run_client import RuntimeClient

from tests.api.app import CHAT, auth, make_app, sse_events, token
from tests.fakes.clock import FakeClock
from tests.fakes.scripted_chat_model import Turn
from zonic_runtime.api.sse import sse_frames
from zonic_runtime.errors import ZonicError
from zonic_runtime.main import create_openapi
from zonic_runtime.models.events import RunEvent

ROOT = Path(__file__).resolve().parents[2]
EVENT = TypeAdapter(RunEvent)


def start(text: str = "hello there", mode: str = "stream", **extra: Any) -> dict[str, Any]:
    return {"agenticWorkflowId": "wf-chat", "input": {"text": text}, "mode": mode, **extra}


# --- A-01, A-02 --------------------------------------------------------------------------------


async def test_A01_the_catalog_is_the_same_for_every_authenticated_caller() -> None:
    app = make_app(workflows={"wf-chat": CHAT, "wf-two": CHAT})
    async with app.client() as client:
        lists = [
            (await client.get("/v1/catalog/agents", headers=auth(user, groups=groups))).json()
            for user, groups in [("alice", ()), ("bob", ("runtime-admin",))]
        ]
    assert lists[0] == lists[1]
    assert sorted(a["agenticWorkflowId"] for a in lists[0]["items"]) == ["wf-chat", "wf-two"]


def _v1_routes() -> list[tuple[str, str]]:
    paths = create_openapi()["paths"]
    found = []
    for path, methods in paths.items():
        if path.startswith("/v1"):
            for method in methods:
                url = path.replace("{runId}", "00000000-0000-0000-0000-000000000001").replace(
                    "{sessionId}", "00000000-0000-0000-0000-000000000002"
                )
                found.append((method.upper(), url))
    return found


@pytest.mark.parametrize(("method", "url"), _v1_routes())
async def test_A02_every_v1_route_needs_a_token(method: str, url: str) -> None:
    app = make_app()
    async with app.client() as client:
        response = await client.request(method, url, json={} if method in ("POST", "PATCH") else None)
    assert response.status_code == 401 and response.json()["error"]["code"] == "AUTH_REQUIRED"


@pytest.mark.parametrize(
    "bad",
    [
        {"audience": "another-client"},
        {"issuer": "https://elsewhere.test"},
        {"expires_in": -3600},
        {"key": rsa.generate_private_key(public_exponent=65537, key_size=2048)},
    ],
    ids=["audience", "issuer", "expired", "signature"],
)
async def test_A02_a_token_with_a_wrong_audience_issuer_signature_or_expiry_is_invalid(
    bad: dict[str, Any],
) -> None:
    app = make_app()
    async with app.client() as client:
        response = await client.get("/v1/catalog/agents", headers={"Authorization": f"Bearer {token(**bad)}"})
        garbage = await client.get("/v1/catalog/agents", headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401 and response.json()["error"]["code"] == "AUTH_INVALID"
    assert garbage.status_code == 401 and garbage.json()["error"]["code"] == "AUTH_INVALID"


async def test_A02_any_client_of_a_comma_separated_audience_is_accepted() -> None:
    from tests.api.app import _KEY, ISSUER, KID
    from tests.api.app import DEV as RESOURCES
    from zonic_runtime.api.auth import StaticKeys, TokenVerifier

    verifier = TokenVerifier(
        StaticKeys({KID: _KEY.public_key()}),
        RESOURCES.settings.auth,
        issuer=ISSUER,
        audience="ui, test-client",
    )
    for client in ("ui", "test-client"):
        assert (await verifier.verify(token("ada", audience=client))).subject == "ada"
    with pytest.raises(ZonicError) as caught:
        await verifier.verify(token("ada", audience="client-1"))
    assert caught.value.code == "AUTH_INVALID"


# --- A-03, A-04 --------------------------------------------------------------------------------


async def test_A03_another_users_session_or_run_is_not_found() -> None:
    app = make_app([Turn.say("Private answer.")])
    async with app.client() as client:
        response = await client.post("/v1/runs", json=start(mode="sync"), headers=auth("alice"))
        run = response.json()
        for url in (
            f"/v1/sessions/{run['sessionId']}",
            f"/v1/sessions/{run['sessionId']}/messages",
            f"/v1/runs/{run['runId']}",
            f"/v1/runs/{run['runId']}/nodes",
            f"/v1/runs/{run['runId']}/events",
        ):
            foreign = await client.get(url, headers=auth("bob"))
            assert foreign.status_code == 404 and foreign.json()["error"]["code"] == "NOT_FOUND", url
            assert "Private" not in foreign.text
        rename = await client.patch(
            f"/v1/sessions/{run['sessionId']}", json={"title": "x"}, headers=auth("bob")
        )
        assert rename.status_code == 404
        reuse = await client.post(
            "/v1/runs", json=start(mode="sync", sessionId=run["sessionId"]), headers=auth("bob")
        )
        assert reuse.status_code == 404


async def test_A04_session_paging_is_stable_without_duplicates_and_title_search_matches_only() -> None:
    app = make_app()
    async with app.client() as client:
        for i in range(23):
            title = f"Invoice {i:02d}" if i % 3 == 0 else f"Travel plan {i:02d}"
            created = await client.post(
                "/v1/sessions", json={"agenticWorkflowId": "wf-chat", "title": title}, headers=auth()
            )
            assert created.status_code == 201
        seen: list[str] = []
        cursor = None
        while True:
            params: dict[str, Any] = {"limit": 5, **({"cursor": cursor} if cursor else {})}
            page = (await client.get("/v1/sessions", params=params, headers=auth())).json()
            seen += [s["sessionId"] for s in page["items"]]
            cursor = page.get("nextCursor")
            if cursor is None:
                break
        everything = (await client.get("/v1/sessions", params={"limit": 100}, headers=auth())).json()["items"]
        assert seen == [s["sessionId"] for s in everything] and len(set(seen)) == 23
        created_at = [s["createdAt"] for s in everything]
        assert created_at == sorted(created_at, reverse=True)
        found = (
            await client.get("/v1/sessions", params={"q": "invoice", "limit": 100}, headers=auth())
        ).json()["items"]
        assert len(found) == 8 and all("Invoice" in s["title"] for s in found)
        literal = (await client.get("/v1/sessions", params={"q": "%"}, headers=auth())).json()["items"]
        assert literal == []
        bad = await client.get("/v1/sessions", params={"cursor": "not-a-cursor"}, headers=auth())
        assert bad.status_code == 400 and bad.json()["error"]["details"][0]["path"] == "cursor"


# --- A-05, A-06, A-07 --------------------------------------------------------------------------


async def test_A05_a_run_without_session_creates_one_and_bad_messages_are_rejected() -> None:
    app = make_app([Turn.say("First answer.")], input={"max_chars": 50})
    async with app.client() as client:
        created = (
            await client.post("/v1/runs", json=start("Plan my trip to Oslo", "sync"), headers=auth())
        ).json()
        session = (await client.get(f"/v1/sessions/{created['sessionId']}", headers=auth())).json()
        assert created["status"] == "succeeded" and session["title"] == "Plan my trip to Oslo"
        empty = await client.post("/v1/runs", json=start("   ", "sync"), headers=auth())
        assert empty.status_code == 400 and empty.json()["error"]["code"] == "INPUT_INVALID"
        assert empty.json()["error"]["details"][0]["path"] == "input.text"
        long = await client.post("/v1/runs", json=start("x" * 51, "sync"), headers=auth())
        assert long.status_code == 413 and long.json()["error"]["code"] == "INPUT_TOO_LARGE"
        neither = await client.post(
            "/v1/runs", json={"input": {"text": "hi"}, "mode": "sync"}, headers=auth()
        )
        assert neither.status_code == 400


async def test_A06_a_repeated_idempotency_key_returns_the_same_run_with_one_row() -> None:
    app = make_app([Turn.say("Once only.")])
    async with app.client() as client:
        headers = {**auth(), "Idempotency-Key": "key-123"}
        first = (await client.post("/v1/runs", json=start(mode="sync"), headers=headers)).json()
        again = (await client.post("/v1/runs", json=start(mode="sync"), headers=headers)).json()
    assert first == again and first["status"] == "succeeded"
    assert len(app.schema.t("runtime_run")) == 1 and len(app.models.model.requests) == 1


async def test_A06_an_idempotency_key_older_than_its_ttl_starts_a_new_run() -> None:
    app = make_app([Turn.say("First."), Turn.say("Second.")])
    async with app.client() as client:
        headers = {**auth(), "Idempotency-Key": "key-ttl"}
        first = (await client.post("/v1/runs", json=start(mode="sync"), headers=headers)).json()
        ttl = app.deps.resources.settings.runs.idempotency_ttl_seconds
        [row] = app.schema.t("runtime_run")
        row["created_at"] = (datetime.now(UTC) - timedelta(seconds=ttl + 1)).isoformat()
        again = (await client.post("/v1/runs", json=start(mode="sync"), headers=headers)).json()
    assert again["runId"] != first["runId"] and again["result"] == {"answer": "Second."}
    assert [r["idempotency_key"] for r in app.schema.t("runtime_run")].count("key-ttl") == 1


async def test_A07_a_second_run_on_a_session_with_an_active_run_is_run_in_progress() -> None:
    app = make_app(
        [Turn.say("Slow answer.", delay=0.5), Turn.say("Second.")], runs={"sync_wait_seconds": 0.05}
    )
    async with app.client() as client:
        accepted = await client.post("/v1/runs", json=start(mode="sync"), headers=auth())
        assert accepted.status_code == 202 and accepted.json()["links"]["events"].endswith("/events")
        session_id = accepted.json()["sessionId"]
        busy = await client.post("/v1/runs", json=start(mode="sync", sessionId=session_id), headers=auth())
        assert busy.status_code == 409 and busy.json()["error"]["code"] == "RUN_IN_PROGRESS"
        await app.deps.registry.wait(accepted.json()["runId"], 5)
        later = await client.post("/v1/runs", json=start(mode="sync", sessionId=session_id), headers=auth())
        assert later.status_code == 200 and later.json()["status"] == "succeeded"


# --- A-09, A-10, A-11, A-12, A-14 ---------------------------------------------------------------


async def test_A09_an_unknown_request_field_is_input_invalid_with_its_path() -> None:
    app = make_app()
    async with app.client() as client:
        response = await client.post(
            "/v1/runs", json={**start(), "input": {"text": "hi", "colour": "red"}}, headers=auth()
        )
        top = await client.post(
            "/v1/sessions", json={"agenticWorkflowId": "wf-chat", "owner": "x"}, headers=auth()
        )
    assert response.status_code == 400 and response.json()["error"]["code"] == "INPUT_INVALID"
    assert [d["path"] for d in response.json()["error"]["details"]] == ["input.colour"]
    assert [d["path"] for d in top.json()["error"]["details"]] == ["owner"]


def test_A10_the_openapi_document_matches_the_committed_snapshot() -> None:
    committed = json.loads((ROOT / "generated" / "openapi.json").read_text(encoding="utf-8"))
    assert create_openapi() == committed
    assert {"RunEvent", "StartRunRequest", "RunResult", "ErrorEnvelope"} <= set(
        committed["components"]["schemas"]
    )


async def test_A11_every_error_response_is_the_envelope_with_a_catalog_code_and_trace_id() -> None:
    app = make_app()
    async with app.client() as client:
        responses = [
            await client.get("/v1/catalog/agents"),
            await client.get("/v1/sessions/00000000-0000-0000-0000-000000000009", headers=auth()),
            await client.get("/v1/sessions/not-a-uuid", headers=auth()),
            await client.post("/v1/runs", json={"mode": "sideways"}, headers=auth()),
            await client.get("/v1/nowhere", headers=auth()),
            await client.post(
                "/v1/runs",
                content=b"x" * (app.deps.resources.settings.api.max_body_bytes + 1),
                headers={**auth(), "content-type": "application/json"},
            ),
            await client.get("/v1/catalog/agents", headers={**auth(), "X-Request-ID": "req-42"}),
        ]
    codes = []
    for response in responses[:-1]:
        error = response.json()["error"]
        assert (
            set(error) >= {"code", "message", "traceId"} and error["code"] in app.deps.resources.errors.errors
        )
        assert response.status_code == app.deps.resources.errors.errors[error["code"]].status
        assert response.headers["x-request-id"] == error["traceId"]
        codes.append(error["code"])
    assert codes == [
        "AUTH_REQUIRED",
        "NOT_FOUND",
        "INPUT_INVALID",
        "INPUT_INVALID",
        "NOT_FOUND",
        "INPUT_TOO_LARGE",
    ]
    assert responses[-1].headers["x-request-id"] == "req-42"


async def test_A12_over_the_rate_limit_is_429_with_retry_after() -> None:
    app = make_app(rate_limit={"per_user_per_minute": 2})
    async with app.client() as client:
        statuses = [(await client.get("/v1/catalog/agents", headers=auth())).status_code for _ in range(3)]
        limited = await client.get("/v1/catalog/agents", headers=auth())
        other = await client.get("/v1/catalog/agents", headers=auth("bob"))
    assert statuses == [200, 200, 429] and limited.json()["error"]["code"] == "RATE_LIMITED"
    assert int(limited.headers["Retry-After"]) >= 1 and other.status_code == 200


def test_A14_the_published_event_schemas_are_the_committed_files() -> None:
    from scripts.gen import expected_files

    published = {
        p: text for p, text in expected_files().items() if "schemas" in p.parts and "events" in p.parts
    }
    assert len(published) == 22
    for path, text in published.items():
        assert path.read_text(encoding="utf-8") == text, path.name


# --- A-15 --------------------------------------------------------------------------------------


async def test_A15_the_test_client_lists_starts_streams_and_continues_a_session() -> None:
    app = make_app([Turn.say("Hello, Ada."), Turn.say("Still here.")])
    async with app.client() as http:
        client = RuntimeClient(http, token("ada"))
        assert [a["agenticWorkflowId"] for a in await client.catalog()] == ["wf-chat"]
        first = await client.run("Hi, I am Ada", workflow_id="wf-chat")
        assert (
            first.final is not None and first.final["type"] == "run.completed" and first.text == "Hello, Ada."
        )
        second = await client.run("Are you there?", session_id=first.session_id)
        assert (
            second.session_id == first.session_id
            and second.final
            and second.final["data"]["result"] == {"answer": "Still here."}
        )
        messages = await client.messages(first.session_id)
    assert [m["textPreview"] for m in reversed(messages) if m["role"] == "user"] == [
        "Hi, I am Ada",
        "Are you there?",
    ]


async def test_A15_the_test_client_answers_an_interrupt_and_the_run_completes() -> None:
    from tests.api.test_interrupts import APPROVAL, DRAFT, FLOW

    app = make_app([Turn.say(DRAFT)], workflows={FLOW: APPROVAL})
    asked: list[dict[str, Any]] = []
    async with app.client() as http:
        client = RuntimeClient(http, token("ada"))
        paused = await client.run("refund order 1042", workflow_id=FLOW)
        assert paused.waiting
        done = await client.resume(paused, lambda i: asked.append(i) or {"approved": True})
    assert [i["kind"] for i in asked] == ["approval"] and asked[0]["answerSchema"]
    assert done.final is not None and done.final["type"] == "run.completed", done.final
    assert done.run_id == paused.run_id and not done.waiting


async def test_A15_the_test_client_leaves_a_run_waiting_without_an_answer() -> None:
    from tests.api.test_interrupts import APPROVAL, DRAFT, FLOW

    app = make_app([Turn.say(DRAFT)], workflows={FLOW: APPROVAL})
    async with app.client() as http:
        client = RuntimeClient(http, token("ada"))
        paused = await client.run("refund order 1042", workflow_id=FLOW)
        left = await client.resume(paused, lambda _: None)
        pending = await client.interrupts(paused.run_id)
    assert left.waiting and len(pending) == 1


async def test_A15_the_test_client_pages_a_dataset_the_run_names() -> None:
    from tests.fakes.mcp_server import FakeMcpServer, ok
    from tests.unit.test_datasets import app_with, rows

    app = app_with(FakeMcpServer(answers={"q": ok(None, rows(30))}), "database")
    async with app.client() as http:
        client = RuntimeClient(http, auth()["Authorization"].removeprefix("Bearer "))
        run = await client.run("Show the sites", workflow_id="wf-db")
        [dataset_id] = run.dataset_ids
        page = await client.dataset(
            dataset_id, page=2, page_size=10, sort="kwh:desc", filter={"borough": "queens"}
        )
    assert page["total"] == 15 and page["page"] == 2 and len(page["rows"]) == 5
    assert [r["kwh"] for r in page["rows"]] == [80, 60, 40, 20, 0]


# --- S-01, S-06, S-07, S-08, S-10, M-06 --------------------------------------------------------


async def test_S01_stream_mode_headers() -> None:
    app = make_app([Turn.say("Streamed.")])
    async with app.client() as client:
        response = await client.post("/v1/runs", json=start(), headers=auth())
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache" and response.headers["x-accel-buffering"] == "no"
    assert response.headers["x-run-id"] and response.headers["x-session-id"]
    frames = response.text.split("\n\n")
    assert frames[0].startswith("id: 1\nevent: run.started\ndata: ")


async def test_S06_heartbeats_flow_while_nothing_else_happens() -> None:
    async def slow() -> Any:
        yield {"seq": 1, "type": "run.started", "runId": "r", "data": {}}
        await asyncio.sleep(0.35)
        yield {"seq": 2, "type": "run.completed", "runId": "r", "data": {}}

    frames = [f.decode() async for f in sse_frames(slow(), "r", 0.1, FakeClock())]
    beats = [f for f in frames if "event: heartbeat" in f]
    assert 2 <= len(beats) <= 4 and all("id:" not in f for f in beats)
    assert frames[0].startswith("id: 1") and frames[-1].startswith("id: 2")


async def test_S07_a_disconnect_mid_run_does_not_stop_the_run_and_it_is_saved() -> None:
    app = make_app([Turn.say("word " * 40, chunk_delay=0.01)])
    async with app.client() as client:
        caller = await app.deps.verifier.verify(token())
        from zonic_runtime.models.api.requests import StartRunRequest

        started = await app.deps.runs.start(caller, StartRunRequest.model_validate(start()))
        frames = sse_frames(started.events, started.run_id, 15, FakeClock())  # type: ignore[union-attr]
        first = [await anext(frames) for _ in range(3)]
        await frames.aclose()  # the client went away
        outcome = await app.deps.registry.wait(started.run_id, 5)  # type: ignore[union-attr]
        run = (await client.get(f"/v1/runs/{started.run_id}", headers=auth())).json()  # type: ignore[union-attr]
    assert len(first) == 3 and outcome is not None and outcome.status == "succeeded"
    assert run["status"] == "succeeded" and run["result"]["answer"].startswith("word")


async def test_S08_reconnect_after_last_event_id_after_the_end_and_after_buffer_expiry() -> None:
    app = make_app([Turn.say("Reconnect me.")])
    async with app.client() as client:
        stream = await client.post("/v1/runs", json=start(), headers=auth())
        events = sse_events(stream.text)
        run_id = events[0]["runId"]
        later = await client.get(f"/v1/runs/{run_id}/events", headers={**auth(), "Last-Event-ID": "3"})
        rest = sse_events(later.text)
        assert [e["seq"] for e in rest] == [e["seq"] for e in events[3:]] and rest[-1][
            "type"
        ] == "run.completed"
        app.deps.bus._streams.clear()  # the buffer expired (released after its retention)
        gone = sse_events(
            (await client.get(f"/v1/runs/{run_id}/events", headers={**auth(), "Last-Event-ID": "3"})).text
        )
        bad = await client.get(f"/v1/runs/{run_id}/events", headers={**auth(), "Last-Event-ID": "x"})
    assert [e["type"] for e in gone] == ["stream.gap", "run.completed"]
    assert gone[0]["data"] == {"fromSeq": 4} and gone[1]["data"]["result"] == {"answer": "Reconnect me."}
    assert bad.status_code == 400


async def test_S10_two_concurrent_streams_of_different_users_never_cross() -> None:
    def answer(request: Any) -> Turn:
        return Turn.say(f"Answer for {request.last_text}", chunk_delay=0.005)

    app = make_app([answer, answer])
    async with app.client() as client:
        alice, bob = await asyncio.gather(
            client.post("/v1/runs", json=start("alice question"), headers=auth("alice")),
            client.post("/v1/runs", json=start("bob question"), headers=auth("bob")),
        )
    a, b = sse_events(alice.text), sse_events(bob.text)
    assert (
        len({e["runId"] for e in a}) == 1
        and len({e["runId"] for e in b}) == 1
        and a[0]["runId"] != b[0]["runId"]
    )
    assert a[-1]["data"]["result"] == {"answer": "Answer for alice question"}
    assert b[-1]["data"]["result"] == {"answer": "Answer for bob question"}


async def test_M06_every_streamed_event_round_trips_through_its_model() -> None:
    app = make_app([Turn.say("Round trip.")])
    async with app.client() as client:
        events = sse_events((await client.post("/v1/runs", json=start(), headers=auth())).text)
    for event in events:
        parsed = EVENT.validate_python(event)
        assert type(parsed.root).__name__.endswith("Event")
        assert parsed.model_dump(mode="json", by_alias=True, exclude_none=True) == event
