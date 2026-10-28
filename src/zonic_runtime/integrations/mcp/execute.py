"""``McpService``: the MCP port over ``McpPool`` (plan P3-01, cases T-01, T-02).

``execute`` sends the request as the arguments of the server's ``execute`` tool and parses the
reply, JSON in the result's structured content or its text, with ``McpExecuteResponse``. The
``flow`` changes (``flow_updates``) are the server's ``meta.flowUpdates``, then each successful
tool's ``output.data.variable`` bound to ``output.data.value`` (the cache key when the result was
saved to the cache). A protocol-level error, or a reply that is not the contract, is
``TOOL_FAILED``; an unreachable server ``TOOL_UNAVAILABLE`` (the pool), and nothing is applied.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from langchain_core.tools import BaseTool
from pydantic import ValidationError

from zonic_runtime.errors import ZonicError
from zonic_runtime.integrations.mcp.pool import McpPool
from zonic_runtime.models.mcp import McpExecuteRequest, McpExecuteResponse
from zonic_runtime.ports.mcp import McpExecuteResult

FAILED = "TOOL_FAILED"
_FLOW = "flow."
#: The key of an ``output.data`` binding that names its ``flow`` variable (the server's field).
_VARIABLE = "variable"  # principle: allow rule-1-node-type mcp-reply-field
_REASON_CHARS = 500


def flow_updates(response: McpExecuteResponse) -> dict[str, Any]:
    updates: dict[str, Any] = {}

    def put(key: str, value: Any) -> None:
        name = key.removeprefix(_FLOW)
        if name:
            updates[name] = value

    for key, value in response.meta.flow_updates.items():
        put(key, value)
    for result in response.results.values():
        data = result.output.get("data") if isinstance(result.output, Mapping) else None
        if result.status == "success" and isinstance(data, Mapping) and isinstance(data.get(_VARIABLE), str):
            put(data[_VARIABLE], data.get("value"))
    return updates


def parse_reply(raw: Any, server_id: str) -> McpExecuteResponse:
    content = getattr(raw, "content", None) or []
    text = "".join(getattr(part, "text", "") for part in content if getattr(part, "type", "") == "text")
    if getattr(raw, "is_error", False):
        raise ZonicError(FAILED, server_id=server_id, reason=text[:_REASON_CHARS] or "execute failed")
    plain = getattr(raw, "structured_content", None)
    if not isinstance(plain, Mapping) or "status" not in plain:
        try:
            plain = json.loads(text)
        except ValueError as exc:
            raise ZonicError(FAILED, server_id=server_id, reason="execute reply is not JSON") from exc
    try:
        return McpExecuteResponse.model_validate(plain)
    except ValidationError as exc:
        issues = [{"path": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()]
        raise ZonicError(FAILED, server_id=server_id, issues=issues) from exc


class McpService:
    def __init__(self, pool: McpPool) -> None:
        self.pool = pool

    @property
    def version(self) -> int:
        return self.pool.version

    async def execute(self, request: McpExecuteRequest, server_id: str | None = None) -> McpExecuteResult:
        found = self.pool.server_id(server_id, execute=True)
        tool = self.pool.server(found).execute_tool or ""
        raw = await self.pool.call(found, tool, request.wire())
        response = parse_reply(raw, found)
        return McpExecuteResult(
            status=response.status, results=response.results, flow_updates=flow_updates(response)
        )

    async def tools(self, server_id: str | None = None) -> Sequence[BaseTool]:
        return await self.pool.tools(server_id)

    async def call(self, server_id: str | None, tool: str, arguments: Mapping[str, Any]) -> Any:
        return await self.pool.call(server_id, tool, arguments)

    async def resource(self, server_id: str | None, uri: str) -> Any:
        return await self.pool.resource(server_id, uri)

    async def aclose(self) -> None:
        await self.pool.aclose()
