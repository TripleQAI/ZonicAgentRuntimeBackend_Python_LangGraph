"""An in-process MCP server for tests (plan P3-01): FastMCP with the Zonic server's ``execute`` tool.

``execute`` answers each tool of a request from ``answers`` (by the tool's ``config.name``, else
its ``id``), else from ``recordings`` (P3-12: by name, the parameter values the server would
resolve, and the request template, through ``template_aliases`` for a copy in another dialect).
A tool with neither is recorded in ``unrecorded`` and fails the call, so a test never passes on an
unplanned call. ``echo`` is a plain MCP tool for agents. ``pool`` builds an ``McpPool`` that
reaches this server in process.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from tests.fakes.recordings import Recordings, template_hash
from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import load_resources
from zonic_runtime.integrations.mcp.pool import McpPool
from zonic_runtime.integrations.mcp.tool_parameters import resolve_tool_parameters

#: ``tool spec -> {status, output?, error?}`` of one tool.
Answer = Callable[[dict[str, Any]], dict[str, Any]]
CATALOG = load_resources(EnvironmentModel(env="dev")).mcp_servers


def ok(variable: str | None = None, value: Any = None, cached: bool = False) -> Answer:
    """A successful tool in the server's envelope, which every tool type uses (a function tool
    binds no variable: ``variable`` is ``None``), as the live server answers."""

    def answer(_: dict[str, Any]) -> dict[str, Any]:
        output = {"is_cached": cached, "data": {"variable": variable, "value": value}}
        return {"status": "success", "output": output, "meta": {"type": "database", "cached": cached}}

    return answer


def failed(message: str) -> Answer:
    return lambda _: {"status": "error", "error": message, "meta": {"type": "api"}}


@dataclass
class FakeMcpServer:
    answers: dict[str, Answer] = field(default_factory=dict)
    flow_updates: dict[str, Any] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)
    unrecorded: list[str] = field(default_factory=list)
    recordings: Recordings | None = None
    #: The answer of a tool with neither an answer nor a recording (platform scenarios); else it fails.
    fallback: Answer | None = None
    #: A copy's request templates -> the recorded document's (``recordings.template_aliases``).
    template_aliases: dict[str, str] = field(default_factory=dict)
    #: Each tool call answered: its name, type, resolved parameter values and configuration.
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    #: What ``saveToCache`` tools kept, by the key they returned.
    cache: dict[str, Any] = field(default_factory=dict)
    #: Seconds each ``execute`` takes; with it, how many ran at once and how many were cancelled.
    delay: float = 0.0
    in_flight: int = 0
    peak: int = 0
    cancelled: int = 0
    server: FastMCP = field(init=False)

    def __post_init__(self) -> None:
        self.server = FastMCP("zonic-fake")
        fake = self

        @self.server.tool(name="execute")
        async def execute(
            requestId: str,
            context: dict[str, Any] | None = None,
            tools: list[dict[str, Any]] | None = None,
            execution: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            fake.calls.append(
                {
                    "requestId": requestId,
                    "context": context or {},
                    "tools": tools or [],
                    "execution": execution,
                }
            )
            if fake.delay:
                fake.in_flight += 1
                fake.peak = max(fake.peak, fake.in_flight)
                try:
                    await asyncio.sleep(fake.delay)
                except asyncio.CancelledError:
                    fake.cancelled += 1
                    raise
                finally:
                    fake.in_flight -= 1
            results: dict[str, Any] = {}
            for tool in tools or []:
                key = str((tool.get("config") or {}).get("name") or tool["id"])
                fake.tool_calls.append(
                    {
                        "tool": key,
                        "type": tool.get("type"),
                        "parameters": resolve_tool_parameters(tool, context or {}),
                        "config": tool.get("config") or {},
                    }
                )
                answer = fake.answers.get(key)
                if answer is not None:
                    results[tool["id"]] = {"id": tool["id"], **answer(tool)}
                    continue
                recorded = fake.recorded(key, tool, fake.tool_calls[-1]["parameters"])
                if recorded is None and fake.fallback is not None:
                    recorded = fake.fallback(tool)
                if recorded is None:
                    fake.unrecorded.append(key)
                    raise ToolError(f"unrecorded call {key}")
                results[tool["id"]] = {"id": tool["id"], **recorded}
            failures = sum(1 for r in results.values() if r["status"] == "error")
            status = "success" if failures == 0 else "error" if failures == len(results) else "partial"
            return {
                "requestId": requestId,
                "status": status,
                "results": results,
                "meta": {
                    "toolCount": len(results),
                    "completed": len(results) - failures,
                    "flowUpdates": fake.flow_updates,
                },
            }

        @self.server.tool(name="echo")
        async def echo(text: str) -> str:
            """Echo the text back."""
            return f"echo: {text}"

    def recorded(self, name: str, tool: dict[str, Any], parameters: dict[str, Any]) -> dict[str, Any] | None:
        """One tool's result from the recordings, as the server returns it (the Node.js fake's)."""
        config = tool.get("config") or {}
        if self.recordings is None:
            return None
        template = template_hash(config)
        found = self.recordings.find(name, parameters, self.template_aliases.get(template, template))
        if found is None:
            return None
        response = found["response"]
        meta = {"type": tool.get("type"), "durationMs": 1, "cached": False}
        if response["status"] == "error":
            return {"status": "error", "error": response.get("error") or "error", "meta": meta}
        result = config.get("result") or {}
        if result.get("saveToCache") is True and isinstance(result.get("saveToFlowKey"), str):
            key = f"cache:{uuid.uuid4().hex}"
            self.cache[key] = response.get("output")
            output = {"is_cached": True, "data": {"variable": result["saveToFlowKey"], "value": key}}
            return {"status": "success", "output": output, "meta": {**meta, "cached": True}}
        # Function tools return their value as it is; database and API tools in the inline envelope.
        value = response.get("output")
        output = (
            value
            if tool.get("type") == "function"
            else {"is_cached": False, "data": {"variable": None, "value": value}}
        )
        return {"status": "success", "output": output, "meta": meta}

    def pool(self) -> McpPool:
        return McpPool(CATALOG, lambda _: None, connector=lambda _sid, _server, _headers: self.server)


class ServerToolsMcp:
    """An ``McpPort`` whose servers' own tools (platform ``mcp`` tools) answer by MCP tool name from
    ``answers`` (else ``{"ok": true}``); ``execute`` goes to the wrapped port (the Node.js
    ``fakeServers``)."""

    def __init__(
        self, inner: Any, answers: Mapping[str, Any], errors: Mapping[str, str] | None = None
    ) -> None:
        self._inner, self._answers, self._errors = inner, dict(answers), dict(errors or {})
        self.calls: list[tuple[str | None, str, dict[str, Any]]] = []

    @property
    def version(self) -> int:
        return int(self._inner.version)

    async def execute(self, request: Any, server_id: str | None = None) -> Any:
        return await self._inner.execute(request, server_id)

    async def tools(self, server_id: str | None = None) -> Any:
        return []

    async def call(self, server_id: str | None, tool: str, arguments: Mapping[str, Any]) -> Any:
        self.calls.append((server_id, tool, dict(arguments)))
        failed = tool in self._errors
        text = self._errors[tool] if failed else json.dumps(self._answers.get(tool, {"ok": True}))
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)], structured_content=None, is_error=failed
        )

    async def resource(self, server_id: str | None, uri: str) -> Any:
        self.calls.append((server_id, f"resource:{uri}", {}))
        return f"resource {uri}"

    async def aclose(self) -> None:
        await self._inner.aclose()
