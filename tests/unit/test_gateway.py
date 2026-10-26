"""Gateway client, repository and fake (P0-07): cases P-03, P-04, M-07."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import BaseModel, SecretStr

from tests.fakes.in_memory_gateway import InMemoryGateway
from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import load_settings
from zonic_runtime.errors import ZonicError
from zonic_runtime.integrations.gateway.http_gateway import HttpGateway, translate_status
from zonic_runtime.integrations.gateway.repository import GatewayRepository, procedure_arguments
from zonic_runtime.models.base import ZonicModel
from zonic_runtime.models.gateway.wire import Filter, FilterOp, OrderBy
from zonic_runtime.models.gateway_meta import gateway_procedure, gateway_view
from zonic_runtime.models.markers import Column
from zonic_runtime.ports.gateway import GatewayPort

from .conftest import RESOURCES

SETTINGS = load_settings(RESOURCES, "test").gateway
ENV = EnvironmentModel(env="test", gateway_base_url="http://gateway.test", gateway_api_key=SecretStr("k-123"))


@gateway_view(schema="runtime", name="v_run", owner_column="owner_sub")
class RunRow(ZonicModel):
    run_id: Annotated[UUID, Column("run_id")]
    status: Annotated[str, Column("status")]
    seq: Annotated[int, Column("last_event_seq")]


@gateway_view(schema="builder", name="v_agent_export")
class ExportRow(ZonicModel):
    agentic_workflow_id: str
    export_json: dict[str, Any]


class SavePayload(BaseModel):
    status: str
    messages: list[dict[str, Any]]


@gateway_procedure(schema="runtime", name="sp_run_finish", placeholders=1, idempotent=True)
class FinishArgs(BaseModel):
    run_id: UUID
    attempt: int
    save: SavePayload


@gateway_procedure(schema="runtime", name="sp_run_start", placeholders=3)
class StartArgs(BaseModel):
    run: dict[str, Any]
    user_message: dict[str, Any]


# -- bridge: real HttpGateway -> httpx mock transport -> InMemoryGateway ------------

LOGICAL_SCHEMA = {v: k for k, v in SETTINGS.schemas.model_dump().items()}
LOGICAL_DS = {v: k for k, v in SETTINGS.datasources.model_dump().items()}


def bridge(fake: InMemoryGateway) -> httpx.AsyncClient:
    async def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        try:
            if request.url.path.endswith("/view"):
                result = await fake.view(
                    "",
                    LOGICAL_SCHEMA[body["schema"]],
                    body["name"],
                    columns=body.get("columns"),
                    filters=[Filter.model_validate(f) for f in body.get("filters", [])],
                    order_by=[OrderBy.model_validate(o) for o in body.get("order_by", [])],
                    limit=body.get("limit"),
                    offset=body.get("offset"),
                )
                return httpx.Response(200, json=result.model_dump())
            if request.url.path.endswith("/procedure"):
                result = await fake.procedure("", LOGICAL_SCHEMA[body["schema"]], body["name"], body["args"])
                return httpx.Response(200, json=result.model_dump())
            if request.url.path == "/v1/batch":
                from zonic_runtime.models.gateway.wire import BatchItem

                items = []
                for raw in body["items"]:
                    raw["payload"]["schema"] = LOGICAL_SCHEMA[raw["payload"]["schema"]]
                    raw["datasource"] = LOGICAL_DS[raw["datasource"]]
                    items.append(BatchItem.model_validate(raw))
                result = await fake.batch(items, mode=body["mode"])
                return httpx.Response(200, json=result.model_dump())
        except ZonicError as exc:
            return httpx.Response(400, json={"error": exc.code, "type": "QueryError"})
        return httpx.Response(404)

    return httpx.AsyncClient(base_url="http://gateway.test", transport=httpx.MockTransport(handle))


GatewayFactory = Callable[[InMemoryGateway], GatewayPort]
FACTORIES: dict[str, GatewayFactory] = {
    "in-memory": lambda fake: fake,
    "http-bridge": lambda fake: HttpGateway(SETTINGS, ENV, client=bridge(fake)),
}


@pytest.fixture(params=list(FACTORIES))
def repo_and_fake(request: pytest.FixtureRequest) -> tuple[GatewayRepository, InMemoryGateway]:
    fake = InMemoryGateway()
    return GatewayRepository(FACTORIES[request.param](fake)), fake


# -- M-07: rows and owner scope ------------------------------------------------------------------


async def test_M07_rows_map_to_models_and_owner_filter_is_always_added(repo_and_fake: Any) -> None:
    repo, fake = repo_and_fake
    mine, theirs = uuid4(), uuid4()
    fake.put_rows(
        "runtime",
        "v_run",
        [
            {"run_id": str(mine), "status": "running", "last_event_seq": 4, "owner_sub": "alice"},
            {"run_id": str(theirs), "status": "running", "last_event_seq": 1, "owner_sub": "bob"},
        ],
    )
    rows = await repo.read(RunRow, owner="alice")
    assert [r.run_id for r in rows] == [mine]
    assert rows[0].seq == 4


async def test_M07_owner_scoped_read_without_owner_is_rejected(repo_and_fake: Any) -> None:
    repo, _ = repo_and_fake
    with pytest.raises(ValueError, match="owner-scoped"):
        await repo.read(RunRow)


async def test_M07_missing_required_column_fails_clearly(repo_and_fake: Any) -> None:
    repo, fake = repo_and_fake
    fake.put_rows("runtime", "v_run", [{"run_id": str(uuid4()), "owner_sub": "alice"}])
    with pytest.raises(ZonicError) as info:
        await repo.read(RunRow, owner="alice")
    assert info.value.code == "DATA_REQUEST_INVALID"
    assert info.value.details["model"] == "RunRow"


async def test_M07_filters_order_and_limit(repo_and_fake: Any) -> None:
    repo, fake = repo_and_fake
    fake.put_rows(
        "builder",
        "v_agent_export",
        [{"agentic_workflow_id": f"wf-{i}", "export_json": {"n": i}} for i in range(5)],
    )
    rows = await repo.read(
        ExportRow,
        filters=[Filter(column="agentic_workflow_id", op=FilterOp.IN, value=["wf-1", "wf-3", "wf-4"])],
        order_by=[OrderBy(column="agentic_workflow_id", descending=True)],
        limit=2,
    )
    assert [r.agentic_workflow_id for r in rows] == ["wf-4", "wf-3"]


# -- P-04: argument encoding and placeholders -------------------------------------------------


def test_P04_positional_arguments_json_text_and_inout_placeholders() -> None:
    run_id = uuid4()
    args = procedure_arguments(
        FinishArgs(
            run_id=run_id, attempt=2, save=SavePayload(status="succeeded", messages=[{"role": "user"}])
        )
    )
    assert args[0] == str(run_id)
    assert args[1] == 2
    assert json.loads(args[2]) == {"status": "succeeded", "messages": [{"role": "user"}]}
    assert args[3:] == [None]
    assert len(procedure_arguments(StartArgs(run={"a": 1}, user_message={}))) == 2 + 3


async def test_P04_procedure_returns_inout_outputs(repo_and_fake: Any) -> None:
    repo, fake = repo_and_fake
    seen: list[list[Any]] = []

    async def handler(_: InMemoryGateway, args: list[Any]) -> list[dict[str, Any]]:
        seen.append(args)
        return [{"o_run_id": "r1", "o_session_id": "s1", "o_message_id": "m1"}]

    fake.on_procedure("runtime", "sp_run_start", handler)
    out = await repo.call(StartArgs(run={"mode": "stream"}, user_message={"text": "hi"}))
    assert out == {"o_run_id": "r1", "o_session_id": "s1", "o_message_id": "m1"}
    assert json.loads(seen[0][0]) == {"mode": "stream"}
    assert seen[0][2:] == [None, None, None]


async def test_P04_batch_items_resolve_logical_names_on_the_wire() -> None:
    sent: list[dict[str, Any]] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"mode": "sequential", "results": []})

    gateway = HttpGateway(
        SETTINGS, ENV, client=httpx.AsyncClient(base_url="http://g", transport=httpx.MockTransport(handle))
    )
    repo = GatewayRepository(gateway)
    items = [
        repo.view_item("runs", RunRow, owner="alice", limit=1),
        repo.procedure_item("start", StartArgs(run={}, user_message={})),
    ]
    await gateway.batch(items, mode="sequential")
    wire = sent[0]
    assert wire["mode"] == "sequential"
    assert {i["datasource"] for i in wire["items"]} == {SETTINGS.datasources.runtime}
    assert {i["payload"]["schema"] for i in wire["items"]} == {SETTINGS.schemas.runtime}
    assert wire["items"][0]["payload"]["filters"][-1] == {"column": "owner_sub", "op": "eq", "value": "alice"}


async def test_P04_authorization_header_and_request_shape() -> None:
    seen: list[httpx.Request] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"rows": [], "row_count": 0, "elapsed_ms": 1.0})

    gateway = HttpGateway(
        SETTINGS,
        ENV,
        client=httpx.AsyncClient(
            base_url="http://g",
            transport=httpx.MockTransport(handle),
            headers={"authorization": "Bearer k-123"},
        ),
    )
    await gateway.view("runtime", "runtime", "v_run", limit=5)
    request = seen[0]
    assert request.url.path == f"/v1/datasources/{SETTINGS.datasources.runtime}/view"
    assert json.loads(request.content)["schema"] == SETTINGS.schemas.runtime
    assert json.loads(request.content)["limit"] == 5
    default_client_gateway = HttpGateway(SETTINGS, ENV)
    assert default_client_gateway._client.headers["authorization"] == "Bearer k-123"
    await default_client_gateway.aclose()


async def test_P04_view_limit_is_capped_below_the_gateway_row_cap() -> None:
    limits: list[int] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        limits.append(json.loads(request.content)["limit"])
        return httpx.Response(200, json={"rows": [], "row_count": 0, "elapsed_ms": 0})

    gateway = HttpGateway(
        SETTINGS, ENV, client=httpx.AsyncClient(base_url="http://g", transport=httpx.MockTransport(handle))
    )
    await gateway.view("runtime", "runtime", "v_run", limit=10_000_000)
    await gateway.view("runtime", "runtime", "v_run")
    assert limits == [SETTINGS.max_view_rows, SETTINGS.max_view_rows]


# -- P-03: retries and error translation -------------------------------------------------------


def _counting(statuses: list[int]) -> tuple[httpx.AsyncClient, list[int]]:
    calls: list[int] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        status = statuses[min(len(calls), len(statuses) - 1)]
        calls.append(status)
        if status == 0:
            raise httpx.ConnectError("refused", request=request)
        body = (
            {"rows": [], "row_count": 0, "elapsed_ms": 0}
            if status < 400
            else {"error": "busy", "type": "PoolTimeoutError"}
        )
        return httpx.Response(status, json=body)

    return httpx.AsyncClient(base_url="http://g", transport=httpx.MockTransport(handle)), calls


async def test_P03_read_retried_on_503_and_transport_errors_then_succeeds() -> None:
    client, calls = _counting([503, 0, 200])
    gateway = HttpGateway(SETTINGS, ENV, client=client)
    await gateway.view("runtime", "runtime", "v_run")
    assert calls == [503, 0, 200]


async def test_P03_read_gives_up_with_data_unavailable() -> None:
    client, calls = _counting([503])
    gateway = HttpGateway(SETTINGS, ENV, client=client)
    with pytest.raises(ZonicError) as info:
        await gateway.view("runtime", "runtime", "v_run")
    assert info.value.code == "DATA_UNAVAILABLE"
    assert len(calls) == SETTINGS.read_retries + 1


async def test_P03_procedure_not_retried_unless_idempotent() -> None:
    client, calls = _counting([503, 503, 200])
    gateway = HttpGateway(SETTINGS, ENV, client=client)
    with pytest.raises(ZonicError) as info:
        await gateway.procedure("runtime", "runtime", "sp_run_start", [])
    assert info.value.code == "DATA_UNAVAILABLE"
    assert calls == [503]  # a write is not retried
    await gateway.procedure("runtime", "runtime", "sp_run_finish", [], retry=True)
    assert calls == [503, 503, 200]  # an idempotent write is


async def test_P03_client_errors_are_not_retried() -> None:
    client, calls = _counting([400])
    gateway = HttpGateway(SETTINGS, ENV, client=client)
    with pytest.raises(ZonicError) as info:
        await gateway.view("runtime", "runtime", "v_run")
    assert info.value.code == "DATA_REQUEST_INVALID"
    assert calls == [400]


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (409, {"error": "owner_mismatch", "type": "IntegrityError"}, "NOT_FOUND"),
        (409, {"error": "run_not_found", "type": "IntegrityError"}, "NOT_FOUND"),
        (409, {"error": "run_in_progress", "type": "IntegrityError"}, "RUN_IN_PROGRESS"),
        (409, {"error": "run_limit_reached\nDETAIL:  tenant", "type": "IntegrityError"}, "RATE_LIMITED"),
        (409, {"error": "stale_attempt", "type": "IntegrityError"}, "RUN_LEASE_LOST"),
        (
            409,
            {"error": "invalid_transition\nDETAIL:  succeeded -> running", "type": "IntegrityError"},
            "DATA_CONFLICT",
        ),
        (409, {"error": "already_answered", "type": "IntegrityError"}, "INTERRUPT_ALREADY_ANSWERED"),
        (409, {"error": "interrupt_expired", "type": "IntegrityError"}, "INTERRUPT_EXPIRED"),
        (
            409,
            {"error": "duplicate key value violates unique constraint", "type": "IntegrityError"},
            "DATA_CONFLICT",
        ),
        (409, {"error": "my_run_not_found", "type": "IntegrityError"}, "DATA_CONFLICT"),
        (503, {"error": "pool timeout", "type": "PoolTimeoutError"}, "DATA_UNAVAILABLE"),
        (400, {"error": "syntax", "type": "QueryError"}, "DATA_REQUEST_INVALID"),
        (404, "not found", "DATA_REQUEST_INVALID"),
    ],
)
def test_P03_gateway_errors_translate_to_catalog_codes(status: int, body: Any, code: str) -> None:
    assert translate_status(status, body).code == code


async def test_P03_in_memory_transactional_batch_rolls_back() -> None:
    fake = InMemoryGateway()
    fake.put_rows("runtime", "v_run", [{"run_id": "r1"}])

    async def append(gw: InMemoryGateway, args: list[Any]) -> list[dict[str, Any]]:
        gw.tables[("runtime", "v_run")].append({"run_id": args[0]})
        return []

    fake.on_procedure("runtime", "sp_append", append)
    repo = GatewayRepository(fake)

    @gateway_procedure(schema="runtime", name="sp_append")
    class Append(BaseModel):
        run_id: str

    @gateway_procedure(schema="runtime", name="sp_missing")
    class Missing(BaseModel):
        x: int

    result = await fake.batch(
        [repo.procedure_item("a", Append(run_id="r2")), repo.procedure_item("b", Missing(x=1))],
        mode="transactional",
    )
    assert result.error_count == 1
    assert fake.tables[("runtime", "v_run")] == [{"run_id": "r1"}]


def test_P04_datetime_and_uuid_arguments_are_text() -> None:
    @gateway_procedure(schema="runtime", name="sp_x")
    class Args(BaseModel):
        at: datetime
        id: UUID

    stamp = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    uid = uuid4()
    assert procedure_arguments(Args(at=stamp, id=uid)) == [stamp.isoformat(), str(uid)]
