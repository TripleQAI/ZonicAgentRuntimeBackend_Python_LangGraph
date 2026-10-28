"""The MCP port (design 7.8; plan P3-01, cases T-01, T-02, T-06, T-08, G-08).

``McpPort`` is process-wide: the servers of ``resources/mcp-servers.json``, one held client each.
Kinds and tools never call it directly; they use the run's ``RunMcp`` (``RunContext.ports.mcp``),
which binds every call to the run: the ``execute`` request carries the run's identity in
``context.system`` and an idempotency key under the run's ID. An unreachable server or a broken
transport is ``TOOL_UNAVAILABLE`` and nothing is applied (T-01); per-tool errors come back in the
results (``partial``, T-02).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from langchain_core.tools import BaseTool

from zonic_runtime.models.mcp import (
    McpExecuteContext,
    McpExecuteRequest,
    McpExecuteTool,
    McpExecution,
    McpToolResult,
)


def mcp_content(raw: Any) -> Any:
    """A tool call's value (the Node.js runtime's ``mcpContent``): the structured content, else
    the text parts joined and parsed as JSON, else that text."""
    structured = getattr(raw, "structured_content", None)
    if structured is not None:
        return structured
    text = "".join(
        getattr(p, "text", "")
        for p in getattr(raw, "content", None) or []
        if getattr(p, "type", "") == "text"
    )
    try:
        return json.loads(text)
    except ValueError:
        return text


@dataclass(frozen=True)
class McpExecuteResult:
    status: str
    results: Mapping[str, McpToolResult] = field(default_factory=dict)
    #: ``flow`` keys (without ``flow.``) and their values: cache keys for cached results.
    flow_updates: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class McpPort(Protocol):
    #: Bumped whenever a server's tool list changes; part of the compiled graphs' cache key (G-08).
    @property
    def version(self) -> int: ...

    async def execute(self, request: McpExecuteRequest, server_id: str | None = None) -> McpExecuteResult: ...

    async def tools(self, server_id: str | None = None) -> Sequence[BaseTool]: ...

    async def call(self, server_id: str | None, tool: str, arguments: Mapping[str, Any]) -> Any: ...

    async def resource(self, server_id: str | None, uri: str) -> Any:
        """A server's resource, read (a platform ``mcp-resource`` tool's reference file)."""
        ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class RunMcp:
    """The MCP port bound to one run (owner-bound, T-08): what tools see through
    ``ToolRuntime.context.ports.mcp``. It holds the run's identity and nothing else."""

    port: McpPort
    run_id: str
    owner_sub: str
    session_id: str | None = None
    tenant_id: str | None = None

    def system(self, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
        found = {"runId": self.run_id, "ownerSub": self.owner_sub}
        if self.session_id:
            found["sessionId"] = self.session_id
        if self.tenant_id:
            found["tenantId"] = self.tenant_id
        return {**dict(extra or {}), **found}

    async def execute(
        self,
        key: str,
        tools: Sequence[McpExecuteTool],
        *,
        flow: Mapping[str, Any] | None = None,
        system: Mapping[str, Any] | None = None,
        execution: McpExecution | None = None,
        server_id: str | None = None,
    ) -> McpExecuteResult:
        """``key`` is the call's place in the run (``nodeId:visit``, or with a call index for an
        agent's tool); the request ID is ``runId:key``, so a retried call is the same request."""
        request = McpExecuteRequest(
            request_id=f"{self.run_id}:{key}",
            context=McpExecuteContext(flow=dict(flow or {}), system=self.system(system)),
            tools=list(tools),
            execution=execution,
        )
        return await self.port.execute(request, server_id)

    async def tools(self, server_id: str | None = None) -> Sequence[BaseTool]:
        return await self.port.tools(server_id)
