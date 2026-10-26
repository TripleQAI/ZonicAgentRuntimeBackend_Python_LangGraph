"""SP-7: ``langchain.mcp.MCPAdapter`` (beta) with an in-process FastMCP server.

Questions (design 20.1):
1. Adapted tools work in ``create_agent``.
2. Pooling: each tool call enters ``async with client`` (reference-counted). Held open by the
   runtime, do calls share one session, including concurrent ones? Not held, does each call
   open its own?
3. Tools changed on the server mid-session: what ``list_tools`` sees with the default cache
   mode and with ``cache_mode="refresh"``.
4. Cancelling a tool call reaches the server, and the session stays usable.
5. A tool that fails reaches the model as an error ``ToolMessage``.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastmcp import Context, FastMCP
from fastmcp.client.transports.memory import FastMCPTransport
from fastmcp.exceptions import ToolError
from langchain.agents import create_agent
from langchain.mcp import MCPAdapter
from langchain_core.messages import HumanMessage, ToolMessage
from tests.fakes.scripted_chat_model import ScriptedChatModel, Turn


def _server() -> tuple[FastMCP, dict[str, Any]]:
    seen: dict[str, Any] = {"sessions": [], "cancelled": 0, "started": 0}
    mcp = FastMCP("spike")

    @mcp.tool
    async def echo(text: str, ctx: Context) -> str:
        """Echo the text."""
        seen["sessions"].append(ctx.session_id)
        return f"echo: {text}"

    @mcp.tool
    async def slow(seconds: float) -> str:
        """Sleep, then answer."""
        seen["started"] += 1
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            seen["cancelled"] += 1
            raise
        return "slept"

    @mcp.tool
    async def broken() -> str:
        """Always fails."""
        raise ToolError("the backend is down")

    return mcp, seen


def _tool(tools: list[Any], name: str) -> Any:
    return next(t for t in tools if t.name == name)


async def test_adapted_tools_in_create_agent() -> None:
    mcp, _ = _server()
    adapter = MCPAdapter(mcp)
    tools = await adapter.list_tools()
    print("\nSP-7.1 tools:", sorted(t.name for t in tools))
    model = ScriptedChatModel(script=[Turn.call("echo", {"text": "hi"}), Turn.say("done")])
    async with adapter:
        out = await create_agent(model, tools=tools).ainvoke({"messages": [HumanMessage("x")]})
    results = [m.text for m in out["messages"] if isinstance(m, ToolMessage)]
    assert results == ["echo: hi"]


async def test_pooling_held_versus_not_held(monkeypatch: pytest.MonkeyPatch) -> None:
    """Counted at the transport: ``ctx.session_id`` is per request under this MCP version."""
    opened = 0
    original = FastMCPTransport.connect_session

    @contextlib.asynccontextmanager
    async def counting(self: FastMCPTransport, **kwargs: Any) -> AsyncIterator[Any]:
        nonlocal opened
        opened += 1
        async with original(self, **kwargs) as session:
            yield session

    monkeypatch.setattr(FastMCPTransport, "connect_session", counting)
    mcp, seen = _server()
    adapter = MCPAdapter(mcp)
    echo = _tool(await adapter.list_tools(), "echo")

    opened = 0
    started = time.perf_counter()
    for i in range(5):
        await echo.ainvoke({"text": str(i)})
    unheld = time.perf_counter() - started
    unheld_connections = opened

    opened = 0
    async with adapter:
        started = time.perf_counter()
        for i in range(5):
            await echo.ainvoke({"text": str(i)})
        held = time.perf_counter() - started
        results = await asyncio.gather(*(echo.ainvoke({"text": str(i)}) for i in range(20)))
    held_connections = opened
    print(
        f"\nSP-7.2 not held: 5 calls opened {unheld_connections} connections, {unheld * 200:.1f} ms/call; "
        f"held: 25 calls (20 concurrent) opened {held_connections}, {held * 200:.1f} ms/call; "
        f"distinct ctx.session_id values {len(set(seen['sessions']))}"
    )
    assert unheld_connections == 5 and held_connections == 1
    assert len(results) == 20


async def test_tools_changed_mid_session() -> None:
    mcp, _ = _server()
    adapter = MCPAdapter(mcp)
    async with adapter:
        before = {t.name for t in await adapter.list_tools()}

        @mcp.tool
        async def added() -> str:
            """Registered after the client connected."""
            return "new"

        await asyncio.sleep(0.05)
        cached = {t.name for t in await adapter.list_tools()}
        refreshed = {t.name for t in await adapter.list_tools(cache_mode="refresh")}
    print(
        f"\nSP-7.3 before {sorted(before)}; after add, cache_mode=use sees 'added': {'added' in cached}; "
        f"refresh sees it: {'added' in refreshed}"
    )
    assert "added" in refreshed


async def test_cancellation_reaches_the_server_and_session_survives() -> None:
    mcp, seen = _server()
    adapter = MCPAdapter(mcp)
    tools = await adapter.list_tools()
    async with adapter:
        task = asyncio.create_task(_tool(tools, "slow").ainvoke({"seconds": 5}))
        while not seen["started"]:
            await asyncio.sleep(0.01)
        started = time.perf_counter()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        for _ in range(100):
            if seen["cancelled"]:
                break
            await asyncio.sleep(0.01)
        reached = time.perf_counter() - started
        after = (await _tool(tools, "echo").ainvoke({"text": "still here"}))[0]["text"]
    print(
        f"\nSP-7.4 server saw cancellation: {bool(seen['cancelled'])} after {reached * 1000:.0f} ms; "
        f"next call: {after!r}"
    )
    assert after == "echo: still here"


async def test_tool_error_reaches_the_model_as_an_error_tool_message() -> None:
    mcp, _ = _server()
    adapter = MCPAdapter(mcp)
    model = ScriptedChatModel(script=[Turn.call("broken", {}), Turn.say("sorry")])
    async with adapter:
        out = await create_agent(model, tools=await adapter.list_tools()).ainvoke(
            {"messages": [HumanMessage("x")]}
        )
    (result,) = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    print(f"\nSP-7.5 error tool message: status={result.status} text={result.text!r}")
    assert result.status == "error" and "backend is down" in result.text
