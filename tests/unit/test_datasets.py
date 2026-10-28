"""The database node and datasets (P3-04, P3-05: N-13, P-12, X-01 datasets part, B-11 grid).

The DB Query node runs through the in-process MCP fake; the cache service is in memory and the MCP
fake writes ``saveToCache`` results into it, as the Zonic MCP server writes into the real one.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from typing import Any

import httpx
import pytest

from tests.api.app import DEV, auth, make_app, sse_events
from tests.fakes.cache import FakeCacheStore
from tests.fakes.egress import FakeEgress
from tests.fakes.mcp_server import FakeMcpServer, failed, ok
from zonic_runtime.application.datasets import joined, rows_of
from zonic_runtime.engine.kinds.database_kind import db_query_tool
from zonic_runtime.engine.node_template import ServicePorts
from zonic_runtime.errors import ZonicError
from zonic_runtime.integrations.cache.http_cache_store import HttpCacheStore
from zonic_runtime.integrations.mcp.execute import McpService
from zonic_runtime.integrations.mcp.pool import McpPool
from zonic_runtime.models.gateway.runtime import DatasetRow
from zonic_runtime.ports.datasets import DatasetOwner, PendingDatasets
from zonic_runtime.ports.egress import EgressRequest, EgressResponse
from zonic_runtime.run.state.dataset_store import DatasetStore, chunks_of

QUERY = "SELECT facility_name, kwh FROM sample_open_data.facility WHERE borough = '<borough>'"


def rows(count: int) -> list[dict[str, Any]]:
    return [
        {"facility_name": f"Site {i}", "kwh": i * 10, "borough": "BROOKLYN" if i % 2 else "QUEENS"}
        for i in range(count)
    ]


def database(store: str = "database", **settings: Any) -> Any:
    datasets = DEV.settings.datasets.model_copy(update={"store": store})
    return DEV.settings.model_copy(update={"datasets": datasets, **settings})


def db_document(output: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    query = {
        "id": "q",
        "type": "db_query",
        "name": "q",
        "config": {
            "query": QUERY,
            "parameters": [{"key": "borough", "value": "BROOKLYN"}, {"key": "unused", "value": "x"}],
            "connectionId": "sample_open_data",
            "databaseType": "postgresql",
        },
    }
    mapping = output or [
        {"key": "count", "value": "{{nodes.q.output.rowCount}}"},
        {"key": "datasetId", "value": "{{nodes.q.output.datasetRef.datasetId}}"},
        {"key": "first", "value": "{{nodes.q.output.rows.0.facility_name}}"},
        {"key": "every", "value": "{{nodes.q.output.rows}}"},
    ]
    return {
        "schemaVersion": "2.0",
        "agenticWorkflowId": "wf-db",
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            query,
            {"id": "out", "type": "output", "config": {"outputMapping": mapping}},
        ],
        "edges": [{"source": "start", "target": "q"}, {"source": "q", "target": "out"}],
    }


def cached_answer(cache: FakeCacheStore, value: list[dict[str, Any]], *, preview: bool = True) -> Any:
    """The MCP server with ``saveToCache``: the rows go to the cache, the reply is the key and a summary."""

    def answer(tool: dict[str, Any]) -> dict[str, Any]:
        result = tool["config"]["result"]
        key = f"mcp:{uuid.uuid4().hex}"
        cache.values[key] = value
        data: dict[str, Any] = {"variable": result["saveToFlowKey"], "value": key}
        if preview:
            size = len(json.dumps(value, separators=(",", ":")).encode())
            data["preview"] = {
                "rows": value[: result["preview"]["rows"]],
                "rowCount": len(value),
                "columns": list(value[0]) if value else [],
                "sizeBytes": size,
            }
        return {"status": "success", "output": {"is_cached": True, "data": data}, "meta": {"cached": True}}

    return answer


async def start(app: Any, workflow: str = "wf-db", subject: str = "alice") -> list[dict[str, Any]]:
    async with app.client() as client:
        body = {"agenticWorkflowId": workflow, "input": {"text": "Show the sites"}, "mode": "stream"}
        response = await client.post("/v1/runs", json=body, headers=auth(subject))
        assert response.status_code == 200, response.text
        return sse_events(response.text)


def app_with(
    mcp: FakeMcpServer, store: str, cache: FakeCacheStore | None = None, egress: Any = None, **docs: Any
) -> Any:
    resources = replace(DEV, settings=database(store))
    ports = ServicePorts(mcp=McpService(mcp.pool()), cache=cache, egress=egress)
    return make_app(resources=resources, workflows={"wf-db": db_document(), **docs}, ports=ports)


# --- N-13 ---------------------------------------------------------------------------------------


def test_N13_the_query_is_one_database_tool_with_only_the_parameters_it_references() -> None:
    config = type(
        "C",
        (),
        {
            "database_type": "postgresql",
            "connection_id": "sample_open_data",
            "max_rows": 500,
            "timeout_seconds": 30,
        },
    )()
    tool = db_query_tool("q", config, QUERY, {"borough": "BROOKLYN", "unused": "x"})
    assert tool.type == "database" and tool.input == {"borough": "BROOKLYN"}
    assert tool.config == {
        "name": "q",
        "queryType": "script",
        "script": QUERY,
        "databaseType": "postgresql",
        "connection": "sample_open_data",  # a registered name, never a connection string (D-7)
        "maxRows": 500,
        "queryTimeout": 30,
        "parameters": [{"name": "borough", "placeholder": "<borough>", "dataType": "string"}],
    }


async def test_N13_rows_come_back_with_a_capped_preview_and_a_dataset_saved_with_the_turn() -> None:
    mcp = FakeMcpServer(answers={"q": ok(None, rows(120))})
    app = app_with(mcp, "database")
    events = await start(app)
    assert events[-1]["type"] == "run.completed", events[-1]
    result = events[-1]["data"]["result"]
    assert result["count"] == 120 and result["first"] == "Site 0"
    assert len(result["every"]) == 120  # later templates read every row while the run holds them
    [request] = mcp.calls
    assert request["requestId"].endswith(":q:1") and "saveToCache" not in json.dumps(request["tools"])
    finished = next(e for e in events if e["type"] == "node.finished" and e.get("nodeId") == "q")
    assert finished["data"]["status"] == "succeeded"
    meta = [r for r in app.schema.t("runtime_dataset") if r["chunk_no"] == 0]
    assert [(m["dataset_id"], m["store"], m["row_count"], m["kind"]) for m in meta] == [
        (result["datasetId"], "database", 120, "rows")
    ]
    assert meta[0]["owner_sub"] == "alice" and meta[0]["node_id"] == "q"


async def test_N13_an_unreachable_server_is_tool_unavailable_never_empty_rows() -> None:
    mcp = FakeMcpServer(answers={"q": ok(None, rows(3))})
    pool = McpPool(DEV.mcp_servers, lambda field: "http://127.0.0.1:9" if field == "mcp_base_url" else None)
    resources = replace(DEV, settings=database())
    app = make_app(
        resources=resources, workflows={"wf-db": db_document()}, ports=ServicePorts(mcp=McpService(pool))
    )
    events = await start(app)
    assert events[-1]["type"] == "run.failed" and events[-1]["data"]["code"] == "TOOL_UNAVAILABLE"
    assert not [
        e
        for e in events
        if e["type"] == "node.finished" and e.get("nodeId") == "q" and e["data"].get("status") == "succeeded"
    ]
    assert mcp.calls == []


async def test_N13_a_failed_query_is_tool_failed() -> None:
    app = app_with(FakeMcpServer(answers={"q": failed("relation does not exist")}), "database")
    events = await start(app)
    assert events[-1]["type"] == "run.failed" and events[-1]["data"]["code"] == "TOOL_FAILED"


async def test_N13_a_cached_result_is_read_back_from_the_cache_and_referenced() -> None:
    cache = FakeCacheStore()
    mcp = FakeMcpServer(answers={"q": cached_answer(cache, rows(30))})
    app = app_with(mcp, "cache", cache)
    events = await start(app)
    assert events[-1]["type"] == "run.completed", events[-1]
    result = events[-1]["data"]["result"]
    assert result["count"] == 30 and len(result["every"]) == 30
    [request] = mcp.calls
    settings = request["tools"][0]["config"]["result"]
    assert settings["saveToCache"] is True and settings["preview"] == {"rows": DEV.settings.io.inline_rows}
    [meta] = [r for r in app.schema.t("runtime_dataset") if r["chunk_no"] == 0]
    assert meta["store"] == "cache" and meta["cache_key"] in cache.values and meta["row_count"] == 30


async def test_N13_a_cached_result_too_large_for_templates_is_described_never_read() -> None:
    cache = FakeCacheStore()
    big = [{"n": i, "text": "x" * 200} for i in range(6000)]  # ~1.3 MB > io.maxResolvedBytes
    app = app_with(FakeMcpServer(answers={"q": cached_answer(cache, big)}), "cache", cache)
    events = await start(app)
    assert events[-1]["type"] == "run.completed", events[-1]
    result = events[-1]["data"]["result"]
    assert result["count"] == 6000 and len(result["every"]) == DEV.settings.io.inline_rows
    assert cache.gets == []  # the summary was enough
    [meta] = [r for r in app.schema.t("runtime_dataset") if r["chunk_no"] == 0]
    assert meta["row_count"] == 6000 and meta["size_bytes"] > DEV.settings.io.max_resolved_bytes


async def test_N13_a_cached_result_that_is_gone_is_data_unavailable() -> None:
    cache = FakeCacheStore()

    def vanished(tool: dict[str, Any]) -> dict[str, Any]:
        found = cached_answer(cache, rows(3), preview=False)(tool)
        cache.values.clear()
        return found

    app = app_with(FakeMcpServer(answers={"q": vanished}), "cache", cache)
    events = await start(app)
    assert events[-1]["type"] == "run.failed" and events[-1]["data"]["code"] == "DATA_UNAVAILABLE"


# --- P-12 ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("store", ["database", "cache"])
async def test_P12_fifty_thousand_rows_are_kept_in_full_and_paged(store: str) -> None:
    cache = FakeCacheStore()
    full = rows(50_000)
    answer = cached_answer(cache, full) if store == "cache" else ok(None, full)
    app = app_with(FakeMcpServer(answers={"q": answer}), store, cache)
    events = await start(app)
    assert events[-1]["type"] == "run.completed", events[-1]
    result = events[-1]["data"]["result"]
    assert result["count"] == 50_000 and len(result["every"]) == DEV.settings.io.inline_rows  # capped
    finished = next(e for e in events if e["type"] == "node.finished" and e.get("nodeId") == "q")
    assert len(json.dumps(finished)) < DEV.settings.io.inline_bytes * 2
    chunks = [r for r in app.schema.t("runtime_dataset") if r["chunk_no"] >= 1]
    assert (len(chunks) > 1) if store == "database" else chunks == []
    async with app.client() as client:
        page = await client.get(
            f"/v1/datasets/{result['datasetId']}", params={"page": 3, "pageSize": 100}, headers=auth()
        )
        assert page.status_code == 200, page.text
        body = page.json()
        assert body["total"] == 50_000 and body["page"] == 3 and body["pageSize"] == 100
        assert [r["facility_name"] for r in body["rows"]][:2] == ["Site 200", "Site 201"]
        assert body["columns"] == ["facility_name", "kwh", "borough"]
        sorted_ = await client.get(
            f"/v1/datasets/{result['datasetId']}",
            params={"sort": "kwh:desc", "filter": json.dumps({"borough": "queens"}), "pageSize": 2},
            headers=auth(),
        )
        assert [r["kwh"] for r in sorted_.json()["rows"]] == [499_980, 499_960]
        assert sorted_.json()["total"] == 25_000


def http_document() -> dict[str, Any]:
    return {
        "schemaVersion": "2.0",
        "agenticWorkflowId": "wf-http",
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {
                "id": "h",
                "type": "http_request",
                "name": "h",
                "config": {"url": "https://data.cityofnewyork.us/resource/x.json", "method": "GET"},
            },
            {
                "id": "out",
                "type": "output",
                "config": {
                    "outputMapping": [
                        {"key": "datasetId", "value": "{{nodes.h.output.datasetRef.datasetId}}"},
                        {"key": "status", "value": "{{nodes.h.output.status}}"},
                    ]
                },
            },
        ],
        "edges": [{"source": "start", "target": "h"}, {"source": "h", "target": "out"}],
    }


@pytest.mark.parametrize("store", ["database", "cache"])
async def test_P12_a_five_megabyte_body_is_kept_in_full_and_paged(store: str) -> None:
    items = [{"id": i, "name": f"item {i}", "pad": "y" * 80} for i in range(50_000)]
    body = json.dumps(items)
    assert len(body) > 5_000_000

    def answer(request: EgressRequest) -> EgressResponse:
        return EgressResponse(
            status=200, headers={"content-type": "application/json"}, content=body.encode(), url=request.url
        )

    cache = FakeCacheStore()
    egress = FakeEgress(DEV.egress, answer=answer)
    app = app_with(FakeMcpServer(), store, cache, egress, **{"wf-http": http_document()})
    events = await start(app, "wf-http")
    assert events[-1]["type"] == "run.completed", events[-1]
    result = events[-1]["data"]["result"]
    assert result["status"] == 200
    # The stream carries the body preview only; every event stays within the SSE limit.
    assert all(len(json.dumps(e).encode()) <= DEV.settings.sse.max_event_bytes * 2 for e in events)
    [meta] = [r for r in app.schema.t("runtime_dataset") if r["chunk_no"] == 0]
    assert meta["kind"] == "http_response" and meta["size_bytes"] > 5_000_000
    if store == "cache":
        assert cache.values[meta["cache_key"]]["body"] == body
    async with app.client() as client:
        page = await client.get(
            f"/v1/datasets/{result['datasetId']}", params={"page": 500, "pageSize": 100}, headers=auth()
        )
        assert page.status_code == 200, page.text
        assert page.json()["total"] == 50_000 and page.json()["rows"][0]["id"] == 49_900


# --- X-01 ---------------------------------------------------------------------------------------


async def test_X01_another_users_dataset_is_the_same_404_as_a_missing_one() -> None:
    app = app_with(FakeMcpServer(answers={"q": ok(None, rows(5))}), "database")
    events = await start(app)
    dataset_id = events[-1]["data"]["result"]["datasetId"]
    async with app.client() as client:
        mine = await client.get(f"/v1/datasets/{dataset_id}", headers=auth())
        theirs = await client.get(f"/v1/datasets/{dataset_id}", headers=auth("mallory"))
        missing = await client.get(f"/v1/datasets/{uuid.uuid4()}", headers=auth())
        garbage = await client.get("/v1/datasets/not-a-uuid", headers=auth())
    assert mine.status_code == 200 and mine.json()["total"] == 5
    for response in (theirs, missing, garbage):
        assert response.status_code == 404 and response.json()["error"]["code"] == "DATASET_NOT_FOUND"


# --- the store, the chunks and the cache service --------------------------------------------------


def test_chunks_join_back_to_the_value() -> None:
    value = rows(5000)
    chunks = chunks_of("rows", value, 20_000)
    assert (
        len(chunks) > 1
        and joined("rows", [DatasetRow.model_validate(_row(c, i)) for i, c in enumerate(chunks)]) == value
    )
    response = {"status": 200, "headers": {"content-type": "text/plain"}, "body": "z" * 50_001}
    parts = chunks_of("http_response", response, 10_000)
    assert len(parts) == 6 and parts[0]["status"] == 200 and "status" not in parts[1]
    assert (
        joined("http_response", [DatasetRow.model_validate(_row(c, i)) for i, c in enumerate(parts)])
        == response
    )
    assert rows_of("http_response", {"body": '{"data": [1, 2]}'}) == [{"value": 1}, {"value": 2}]
    assert rows_of("http_response", {"body": "plain"}) == [{"body": "plain"}]


def _row(body: Any, index: int) -> dict[str, Any]:
    return {
        "dataset_id": str(uuid.UUID(int=1)),
        "chunk_no": index + 1,
        "run_id": str(uuid.UUID(int=2)),
        "session_id": str(uuid.UUID(int=3)),
        "store": "database",
        "kind": "rows",
        "body": body,
    }


async def test_without_a_cache_service_the_cache_store_falls_back_to_the_database() -> None:
    store = DatasetStore(database("cache"))
    assert store.store() == "database" and store.store("cache") == "database"
    owner = DatasetOwner("r", "n", 1, PendingDatasets())
    ref = await store.keep(owner, "rows", rows(3), "cache")
    assert ref.store == "database" and len(owner.pending.chunks) == 1
    with pytest.raises(ZonicError, match="DATA_UNAVAILABLE"):
        await store.adopt(owner, "rows", "mcp:gone")
    again = store.register(owner, "tool_result", "mcp:k")
    assert store.register(owner, "tool_result", "mcp:k").dataset_id == again.dataset_id  # registered once


async def test_the_http_cache_store_speaks_the_mcp_servers_protocol() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "PUT":
            return httpx.Response(201)
        if request.url.path.endswith("missing"):
            return httpx.Response(404)
        if request.url.path.endswith("miss"):
            return httpx.Response(200, json={"data": {"hit": False}})
        if request.url.path.endswith("broken"):
            return httpx.Response(500)
        return httpx.Response(200, json={"data": {"hit": True, "value": [1, 2]}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    store = HttpCacheStore("http://cache.test/api/", DEV.settings.datasets, client)
    await store.put("zonic:dataset:1", {"a": 1}, 60)
    assert seen[0].url.raw_path == b"/api/keys/zonic:dataset:1"
    assert json.loads(seen[0].content) == {"value": {"a": 1}, "ttl": 60}
    assert await store.get("k:hit") == [1, 2]
    assert await store.get("k:missing") is None and await store.get("k:miss") is None
    with pytest.raises(ZonicError, match="DATA_UNAVAILABLE"):
        await store.get("k:broken")
    await client.aclose()


# --- B-11 ---------------------------------------------------------------------------------------


async def test_B11_a_grid_over_a_cache_key_this_turn_registered_names_its_dataset() -> None:
    """A tool's cached result is registered as a dataset; a grid over its key can be paged."""
    from tests.kinds.harness import Harness
    from tests.unit.test_view import GRID, flow, view
    from zonic_runtime.engine.context import RunPorts
    from zonic_runtime.engine.kinds.core import core_kinds
    from zonic_runtime.ports.datasets import RunDatasets

    bound = RunDatasets(DatasetStore(database("cache"), FakeCacheStore()), "run-1")
    registered = bound.port.register(bound.owner("agent", 1), "rows", "bills:v1:abc")
    h = Harness(DEV, core_kinds(), run_ports=lambda run_id: RunPorts(datasets=bound))
    final, *_ = await h.run(
        view(viewMode="template_config", templateConfig=GRID), flow(billsKey="bills:v1:abc")
    )
    [part] = final["nodes"]["v"]["parts"]
    assert (
        part["datasetRef"]["datasetId"] == registered.dataset_id
        and part["datasetRef"]["key"] == "bills:v1:abc"
    )
