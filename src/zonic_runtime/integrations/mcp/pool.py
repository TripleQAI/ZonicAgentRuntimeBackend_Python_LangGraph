"""``McpPool``: one held MCP client per server of ``resources/mcp-servers.json`` (plan P3-01, SP-7;
cases T-06, G-08).

* A server's client is created and entered on first use, then held for the process lifetime, so
  every call of every run shares its connection (SP-7: held, 25 calls use 1 connection); at
  most ``maxInFlight`` calls run at once per server. ``aclose`` exits every client (shutdown).
* Tools come from ``langchain.mcp.MCPAdapter`` around the held client (the adapter is beta; it
  stays behind this pool). A ``tools/list_changed`` notification marks the server's list stale;
  the next ``tools`` call lists again (``cache_mode="refresh"``), and a list that differs bumps
  ``version``, which is part of the compiled graphs' cache key (G-08).
* Endpoints come from the catalog and the environment (``url: "env:<field>"``), never from a
  document; ``authSecretRef: "env:<field>"`` sends that value as a bearer token.
* A server that cannot be reached, or a transport that breaks, is ``TOOL_UNAVAILABLE``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import warnings
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import anyio
import httpx2
from fastmcp.client import Client
from fastmcp.client.messages import MessageHandler
from fastmcp.client.transports import StreamableHttpTransport
from langchain_core._api import LangChainBetaWarning
from langchain_core.tools import BaseTool
from mcp.shared.exceptions import MCPError

from zonic_runtime.errors import ZonicError
from zonic_runtime.models.resources.catalogs import McpServer, McpServerCatalog

with warnings.catch_warnings():
    warnings.simplefilter("ignore", LangChainBetaWarning)
    from langchain.mcp import MCPAdapter

UNAVAILABLE = "TOOL_UNAVAILABLE"
_ENV = "env:"
_DEFAULT = "default"
#: What a server that is down, or a connection that breaks, raises.
TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    RuntimeError,
    OSError,
    TimeoutError,
    MCPError,
    httpx2.HTTPError,
    anyio.ClosedResourceError,
    anyio.BrokenResourceError,
    anyio.EndOfStream,
)
#: ``(server ID, server, headers) -> the MCP target``: a URL transport in production, an
#: in-process server in tests.
Connector = Callable[[str, McpServer, dict[str, str]], Any]


class _Changes(MessageHandler):
    def __init__(self, on_change: Callable[[], None]) -> None:
        super().__init__()
        self._on_change = on_change

    async def on_tool_list_changed(self, message: Any) -> None:
        self._on_change()


@dataclass
class _Held:
    adapter: Any
    limit: asyncio.Semaphore
    tools: list[BaseTool] = field(default_factory=list)
    digest: str = ""
    stale: bool = True


def _digest(tools: Sequence[BaseTool]) -> str:
    shape = [(t.name, t.description, json.dumps(t.args, sort_keys=True, default=str)) for t in tools]
    return hashlib.sha256(json.dumps(sorted(shape)).encode()).hexdigest()


class McpPool:
    def __init__(
        self,
        catalog: McpServerCatalog,
        environment: Callable[[str], str | None],
        *,
        connector: Connector | None = None,
    ) -> None:
        self._catalog = catalog
        self._environment = environment
        self._connector = connector or self._url_target
        self._held: dict[str, _Held] = {}
        self._opening: dict[str, asyncio.Lock] = {}
        self._closed = False
        #: Bumped when any server's tool list changes (G-08).
        self.version = 0
        #: Clients created, by server ID (T-06).
        self.connections: dict[str, int] = {}

    # -- servers ------------------------------------------------------------------------------

    def server_id(self, server_id: str | None, execute: bool = False) -> str:
        """The server for an ID; none (or ``default``) is the ``execute`` server."""
        found = self._catalog.default_execute_server if server_id in (None, "", _DEFAULT) else server_id
        server = self._catalog.servers.get(found or "")
        if server is None or not server.enabled:
            raise ZonicError(UNAVAILABLE, server_id=server_id, reason="no enabled MCP server with this ID")
        if execute and not server.execute_tool:
            raise ZonicError(UNAVAILABLE, server_id=found, reason="server has no execute tool")
        return str(found)

    def server(self, server_id: str) -> McpServer:
        return self._catalog.servers[server_id]

    def _value(self, reference: str) -> str | None:
        return self._environment(reference[len(_ENV) :]) if reference.startswith(_ENV) else reference

    def _url_target(self, server_id: str, server: McpServer, headers: dict[str, str]) -> Any:
        base = self._value(server.url)
        if not base:
            raise ZonicError(UNAVAILABLE, server_id=server_id, reason="the server URL is not configured")
        return StreamableHttpTransport(base.rstrip("/") + server.path, headers=headers)

    async def _hold(self, server_id: str) -> _Held:
        held = self._held.get(server_id)
        if held is not None:
            return held
        lock = self._opening.setdefault(server_id, asyncio.Lock())
        async with lock:
            held = self._held.get(server_id)
            if held is not None:
                return held
            if self._closed:
                raise ZonicError(UNAVAILABLE, server_id=server_id, reason="the MCP pool is closed")
            server = self.server(server_id)
            headers: dict[str, str] = {}
            if server.auth_secret_ref:
                token = self._value(server.auth_secret_ref)
                if token:
                    headers["authorization"] = f"Bearer {token}"
            target = self._connector(server_id, server, headers)

            def stale() -> None:
                found = self._held.get(server_id)
                if found is not None:
                    found.stale = True

            client = Client(
                target,
                timeout=server.timeout_seconds,
                init_timeout=server.timeout_seconds,
                message_handler=_Changes(stale),
            )
            adapter = MCPAdapter(client)
            try:
                await adapter.__aenter__()
            except TRANSPORT_ERRORS as exc:
                raise ZonicError(UNAVAILABLE, server_id=server_id, reason=str(exc)[:300]) from exc
            self.connections[server_id] = self.connections.get(server_id, 0) + 1
            held = self._held[server_id] = _Held(
                adapter=adapter, limit=asyncio.Semaphore(server.max_in_flight)
            )
            return held

    # -- tools and calls --------------------------------------------------------------------------

    async def tools(self, server_id: str | None = None) -> list[BaseTool]:
        found = self.server_id(server_id)
        held = await self._hold(found)
        if held.stale:
            try:
                tools = await held.adapter.list_tools(cache_mode="refresh")
            except TRANSPORT_ERRORS as exc:
                raise ZonicError(UNAVAILABLE, server_id=found, reason=str(exc)[:300]) from exc
            digest = _digest(tools)
            if held.digest and digest != held.digest:
                self.version += 1
            held.tools, held.digest, held.stale = list(tools), digest, False
        return list(held.tools)

    def tools_changed(self, server_id: str | None = None) -> None:
        """A ``tools/list_changed`` notification (also called by the client's message handler)."""
        held = self._held.get(self.server_id(server_id))
        if held is not None:
            held.stale = True

    async def call(self, server_id: str | None, tool: str, arguments: Mapping[str, Any]) -> Any:
        """One tool call: the raw MCP ``CallToolResult`` (``content``, ``structured_content``,
        ``is_error``)."""
        found = self.server_id(server_id)
        held = await self._hold(found)
        async with held.limit:
            try:
                return await held.adapter.client.call_tool(
                    tool,
                    dict(arguments),
                    timeout=self.server(found).timeout_seconds,
                    raise_on_error=False,
                )
            except TRANSPORT_ERRORS as exc:
                raise ZonicError(UNAVAILABLE, server_id=found, tool=tool, reason=str(exc)[:300]) from exc

    async def resource(self, server_id: str | None, uri: str) -> Any:
        """One resource read (a platform ``mcp-resource`` tool): its text contents, joined."""
        found = self.server_id(server_id)
        held = await self._hold(found)
        async with held.limit:
            try:
                contents = await held.adapter.client.read_resource(uri)
            except TRANSPORT_ERRORS as exc:
                raise ZonicError(UNAVAILABLE, server_id=found, uri=uri, reason=str(exc)[:300]) from exc
        return "\n".join(str(getattr(c, "text", "")) for c in contents if getattr(c, "text", None))

    async def aclose(self) -> None:
        self._closed = True
        held, self._held = list(self._held.values()), {}
        for entry in held:
            try:
                await entry.adapter.__aexit__(None, None, None)
            except TRANSPORT_ERRORS:
                continue  # the server is gone already
