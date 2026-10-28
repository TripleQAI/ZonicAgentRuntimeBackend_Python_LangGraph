"""The MCP server's ``execute`` contract (ZonicMCPServer_FastAPI ``app/models/execution.py``; plan P3-01).

One request runs a set of ``database``, ``api`` and ``function`` tools; the reply has one result
per tool ID and the batch status. ``context.flow`` and ``context.system`` resolve the tools'
``{{flow.*}}`` and ``{{system.*}}`` values on the server; ``requestId`` is the idempotency key
``runId:nodeId:visit`` (design 7.6). The shapes are the server's, camelCase on the wire, and the
Node.js runtime's models.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict, Field

from zonic_runtime.models.base import ZonicModel

McpToolType = Literal["database", "api", "function"]


class McpExecuteTool(ZonicModel):
    id: str
    type: McpToolType
    config: dict[str, Any] = Field(default_factory=dict)
    input: dict[str, Any] = Field(default_factory=dict)
    stage: int | None = Field(default=None, ge=0)
    depends_on: list[str] | None = None


class McpExecution(ZonicModel):
    mode: Literal["sequential", "concurrent", "pipeline"] = "sequential"
    max_concurrency: int | None = Field(default=None, gt=0)
    fail_fast: bool | None = None


class McpExecuteContext(ZonicModel):
    flow: dict[str, Any] = Field(default_factory=dict)
    system: dict[str, Any] = Field(default_factory=dict)


class McpExecuteRequest(ZonicModel):
    request_id: str
    context: McpExecuteContext = Field(default_factory=McpExecuteContext)
    tools: list[McpExecuteTool] = Field(default_factory=list)
    execution: McpExecution | None = None


class McpToolMeta(ZonicModel):
    model_config = ConfigDict(extra="ignore")

    type: str = "unknown"
    duration_ms: float | None = None
    #: The output is a cache key, not the data (``result.saveToCache``).
    cached: bool = False


class McpToolResult(ZonicModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    status: Literal["success", "error"]
    #: ``{is_cached, data: {variable, value}}`` for database and API tools; any value for functions.
    output: Any = None
    error: str | None = None
    meta: McpToolMeta = Field(default_factory=McpToolMeta)


class McpExecuteMeta(ZonicModel):
    model_config = ConfigDict(extra="ignore")

    total_duration_ms: float | None = None
    tool_count: int | None = Field(default=None, ge=0)
    completed: int | None = Field(default=None, ge=0)
    #: ``flow`` changes the server reports beside the per-tool ``output.data.variable`` bindings.
    flow_updates: dict[str, Any] = Field(default_factory=dict)


class McpExecuteResponse(ZonicModel):
    """The reply; unknown fields the server may add later are ignored."""

    model_config = ConfigDict(extra="ignore")

    request_id: str | None = None
    #: ``partial``: some tools failed; their results carry the error.
    status: Literal["success", "partial", "error"]
    results: dict[str, McpToolResult] = Field(default_factory=dict)
    meta: McpExecuteMeta = Field(default_factory=McpExecuteMeta)
