"""The MCP pool and the ``execute`` client (P3-01: T-01, T-02, T-06, T-08, G-08), over the real
``McpPool`` and ``MCPAdapter`` with an in-process FastMCP server (``tests/fakes/mcp_server.py``)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from tests.api.app import CHAT, make_app, token
from tests.fakes.mcp_server import CATALOG, FakeMcpServer, failed, ok
from zonic_runtime.engine.node_template import ServicePorts
from zonic_runtime.errors import ZonicError
from zonic_runtime.integrations.mcp.execute import McpService, flow_updates, parse_reply
from zonic_runtime.integrations.mcp.pool import McpPool
from zonic_runtime.models.mcp import McpExecuteRequest, McpExecuteResponse, McpExecuteTool
from zonic_runtime.ports.mcp import RunMcp

SQL = McpExecuteTool(id="t1", type="database", config={"name": "bills_query", "query": "select 1"})
API = McpExecuteTool(id="t2", type="api", config={"name": "weather_api", "method": "GET"})


def run_mcp(service: McpService, run_id: str = "run-1", owner: str = "alice") -> RunMcp:
    return RunMcp(service, run_id, owner, session_id="s-1", tenant_id="t1")


# --- T-01 --------------------------------------------------------------------------------------


async def test_T01_an_unreachable_mcp_server_is_tool_unavailable_quickly() -> None:
    pool = McpPool(CATALOG, lambda field: "http://127.0.0.1:9" if field == "mcp_base_url" else None)
    started = time.monotonic()
    with pytest.raises(ZonicError) as caught:
        await run_mcp(McpService(pool)).execute("db_1:1", [SQL], flow={"x": 1})
    assert caught.value.code == "TOOL_UNAVAILABLE" and time.monotonic() - started < 10
    await pool.aclose()


async def test_T01_an_unconfigured_server_url_is_tool_unavailable() -> None:
    with pytest.raises(ZonicError) as caught:
        await McpService(McpPool(CATALOG, lambda _: None)).tools()
    assert caught.value.code == "TOOL_UNAVAILABLE" and "not configured" in caught.value.details["reason"]


# --- T-02 --------------------------------------------------------------------------------------


async def test_T02_partial_errors_come_back_per_tool_and_flow_is_updated() -> None:
    fake = FakeMcpServer(
        answers={
            "bills_query": ok("flow.billsKey", "cache:bills:v1:abc", cached=True),
            "weather_api": failed("503 upstream"),
        },
        flow_updates={"flow.lastRun": "2026-10-27"},
    )
    service = McpService(fake.pool())
    result = await run_mcp(service).execute("db_1:1", [SQL, API], flow={"region": "east"})
    await service.aclose()
    assert result.status == "partial"
    assert result.results["t1"].status == "success" and result.results["t1"].meta.cached
    assert result.results["t2"].status == "error" and result.results["t2"].error == "503 upstream"
    assert dict(result.flow_updates) == {"lastRun": "2026-10-27", "billsKey": "cache:bills:v1:abc"}
    [call] = fake.calls
    assert call["context"]["flow"] == {"region": "east"} and [t["id"] for t in call["tools"]] == ["t1", "t2"]


async def test_T02_an_unrecorded_tool_fails_the_call_and_nothing_is_applied() -> None:
    fake = FakeMcpServer(answers={})
    service = McpService(fake.pool())
    with pytest.raises(ZonicError) as caught:
        await run_mcp(service).execute("db_1:1", [SQL])
    await service.aclose()
    assert caught.value.code == "TOOL_FAILED" and fake.unrecorded == ["bills_query"]


def test_T02_a_reply_that_is_not_the_contract_is_tool_failed() -> None:
    class Raw:
        is_error = False
        structured_content = None
        content = [type("Text", (), {"type": "text", "text": '{"status": "maybe"}'})()]

    with pytest.raises(ZonicError) as caught:
        parse_reply(Raw(), "zonic")
    assert caught.value.code == "TOOL_FAILED" and caught.value.details["issues"]


def test_T02_flow_updates_skip_failed_tools_and_strip_the_flow_prefix() -> None:
    response = McpExecuteResponse.model_validate(
        {
            "status": "partial",
            "results": {
                "a": {"id": "a", "status": "success", "output": {"data": {"variable": "flow.a", "value": 1}}},
                "b": {"id": "b", "status": "error", "output": {"data": {"variable": "flow.b", "value": 2}}},
                "c": {"id": "c", "status": "success", "output": "plain function value"},
            },
            "meta": {"flowUpdates": {"flow.m": True, "n": 3}},
        }
    )
    assert flow_updates(response) == {"m": True, "n": 3, "a": 1}


# --- T-06 --------------------------------------------------------------------------------------


async def test_T06_one_client_per_server_is_shared_by_concurrent_calls() -> None:
    fake = FakeMcpServer(answers={"bills_query": ok("flow.k", "v")})
    pool = fake.pool()
    service = McpService(pool)
    bound = [run_mcp(service, f"run-{i}") for i in range(25)]
    results = await asyncio.gather(*(b.execute("db_1:1", [SQL]) for b in bound))
    assert all(r.status == "success" for r in results) and len(fake.calls) == 25
    assert pool.connections == {"zonic": 1}
    await service.aclose()


async def test_T06_a_changed_tool_list_bumps_the_catalog_version() -> None:
    fake = FakeMcpServer()
    pool = fake.pool()
    first = await pool.tools()
    assert {t.name for t in first} == {"execute", "echo"} and pool.version == 0
    assert await pool.tools() == first and pool.version == 0  # held, not listed again

    @fake.server.tool(name="weather")
    async def weather(city: str) -> str:
        """The weather of a city."""
        return f"sunny in {city}"

    pool.tools_changed()  # the server's tools/list_changed notification
    again = await pool.tools()
    assert {t.name for t in again} == {"execute", "echo", "weather"} and pool.version == 1
    pool.tools_changed()
    await pool.tools()
    assert pool.version == 1  # the same list again does not bump it
    await pool.aclose()


async def test_T06_adapted_tools_call_the_server_over_the_held_connection() -> None:
    fake = FakeMcpServer()
    pool = fake.pool()
    [echo] = [t for t in await pool.tools() if t.name == "echo"]
    found: Any = await echo.ainvoke({"text": "hi"})
    assert "echo: hi" in str(found) and pool.connections == {"zonic": 1}
    await pool.aclose()


async def test_T06_after_shutdown_every_call_is_tool_unavailable() -> None:
    fake = FakeMcpServer(answers={"bills_query": ok("flow.k", "v")})
    pool = fake.pool()
    service = McpService(pool)
    await run_mcp(service).execute("db_1:1", [SQL])
    await service.aclose()
    with pytest.raises(ZonicError) as caught:
        await run_mcp(service).execute("db_1:2", [SQL])
    assert caught.value.code == "TOOL_UNAVAILABLE" and "closed" in caught.value.details["reason"]


# --- T-08 --------------------------------------------------------------------------------------


async def test_T08_calls_carry_their_runs_identity_and_the_pool_keeps_none() -> None:
    fake = FakeMcpServer(answers={"bills_query": ok("flow.k", "v")})
    service = McpService(fake.pool())
    await asyncio.gather(
        run_mcp(service, "run-a", "alice").execute("db_1:1", [SQL]),
        run_mcp(service, "run-b", "bob").execute("db_1:1", [SQL], system={"userQuery": "q"}),
    )
    by_run = {c["requestId"]: c["context"]["system"] for c in fake.calls}
    assert by_run["run-a:db_1:1"] == {
        "runId": "run-a",
        "ownerSub": "alice",
        "sessionId": "s-1",
        "tenantId": "t1",
    }
    assert by_run["run-b:db_1:1"]["ownerSub"] == "bob" and by_run["run-b:db_1:1"]["userQuery"] == "q"
    held = vars(service.pool)
    assert "run-a" not in repr(held) and "alice" not in repr(held)  # nothing of a run stays in the pool
    await service.aclose()


async def test_T08_the_run_context_holds_an_owner_bound_mcp_port() -> None:
    fake = FakeMcpServer()
    service = McpService(fake.pool())
    app = make_app(ports=ServicePorts(mcp=service))
    caller = await app.deps.verifier.verify(token("carol"))
    from zonic_runtime.models.api.requests import StartRunRequest

    started = await app.deps.runs.start(
        caller,
        StartRunRequest.model_validate(
            {"agenticWorkflowId": "wf-chat", "input": {"text": "hi"}, "mode": "stream"}
        ),
    )
    run = app.deps.registry.get(started.run_id)  # type: ignore[union-attr]
    assert run is not None
    bound = run.context.ports.mcp
    assert isinstance(bound, RunMcp) and bound.port is service
    assert (bound.run_id, bound.owner_sub, bound.tenant_id) == (started.run_id, "carol", "t1")  # type: ignore[union-attr]
    await app.deps.registry.wait(started.run_id, 5)  # type: ignore[union-attr]
    await service.aclose()


# --- G-08 --------------------------------------------------------------------------------------


class VersionedMcp:
    version = 0

    async def aclose(self) -> None:
        return None


async def test_G08_a_cached_graph_is_recompiled_after_the_tool_catalog_changes() -> None:
    mcp = VersionedMcp()
    app = make_app(workflows={"wf-chat": CHAT}, ports=ServicePorts(mcp=mcp))  # type: ignore[arg-type]
    caller = await app.deps.verifier.verify(token())
    runtime = app.deps.runtime
    first = await runtime.prepare("wf-chat", caller)
    again = await runtime.prepare("wf-chat", caller)
    assert again.graph is first.graph and runtime.graphs.compilations == 1
    mcp.version = 1
    changed = await runtime.prepare("wf-chat", caller)
    assert changed.graph is not first.graph and runtime.graphs.compilations == 2


def test_the_execute_request_is_the_servers_camel_case_contract() -> None:
    request = McpExecuteRequest(request_id="r:1", tools=[SQL])
    assert request.wire() == {
        "requestId": "r:1",
        "context": {"flow": {}, "system": {}},
        "tools": [
            {
                "id": "t1",
                "type": "database",
                "config": {"name": "bills_query", "query": "select 1"},
                "input": {},
            }
        ],
    }


async def test_T06_a_tools_list_changed_notification_marks_the_list_stale() -> None:
    from mcp.types import ToolListChangedNotification

    fake = FakeMcpServer()
    pool = fake.pool()
    await pool.tools()
    held = pool._held["zonic"]
    handler = held.adapter.client._base_message_handler  # what the client calls for server messages
    assert held.stale is False and handler is not None
    await handler(ToolListChangedNotification(method="notifications/tools/list_changed"))
    assert held.stale is True
    await pool.aclose()
