"""The ``mcp_execute`` kind (node type ``db_query``, design 7.5; plan P3-04, case N-13), after the
Node.js runtime's ``mcp-execute-node.ts``.

The query runs as one database tool of the MCP ``execute`` tool, on the connection the node names
(``connectionId``: a registered connection name, never a connection string, D-7). Its full result
is a dataset (P3-05): in the ``cache`` store the MCP server writes the rows to the cache itself and
returns a summary (first rows, count, columns, size) with the key, and the node reads the rows
back only when they fit ``io.maxResolvedBytes``; in the ``database`` store the rows come inline
and are saved with the turn. The output holds a preview of at most ``io.inlineRows`` rows and
``io.inlineBytes``, the row count, the columns and the reference; later templates read the full
rows while the run holds them. An unreachable server fails the node with ``TOOL_UNAVAILABLE`` and
a failed query with ``TOOL_FAILED``: never empty rows.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from zonic_runtime.engine.kinds.flow_kinds import _Kind
from zonic_runtime.engine.kinds.io_kinds import mapping_record
from zonic_runtime.engine.node_template import NodeContext
from zonic_runtime.errors import ZonicError
from zonic_runtime.io.rows import columns_of, held_within, preview, rows_of
from zonic_runtime.models.engine import DatasetRef, NodeOutcome
from zonic_runtime.models.io.node_io import NodeInput
from zonic_runtime.models.mcp import McpExecuteTool
from zonic_runtime.ports.datasets import DatasetSummary

FAILED, UNAVAILABLE = "TOOL_FAILED", "TOOL_UNAVAILABLE"
_QUERY, _PARAMETERS = "query", "parameters"
#: The MCP query mode, not a node type.
_SCRIPT = "script"  # principle: allow rule-1-node-type mcp-query-type


def db_query_tool(
    name: str,
    config: Any,
    query: str,
    parameters: Mapping[str, Any],
    result: Mapping[str, Any] | None = None,
) -> McpExecuteTool:
    """A DB Query node as one database tool: the query is the tool's script, and its ``parameters``
    fill the ``<key>`` placeholders the query contains. A parameter the query does not reference
    has no effect on the server, so it is not sent; the recordings (P3-12) are keyed by the node
    name and these values."""
    referenced = {key: value for key, value in parameters.items() if f"<{key}>" in query}
    settings: dict[str, Any] = {"name": name, "queryType": _SCRIPT, _SCRIPT: query}
    for key, value in (
        ("databaseType", getattr(config, "database_type", None)),
        ("connection", getattr(config, "connection_id", None)),
        ("maxRows", getattr(config, "max_rows", None)),
        ("queryTimeout", getattr(config, "timeout_seconds", None)),
    ):
        if value:
            settings[key] = value
    settings["parameters"] = [
        {"name": key, "placeholder": f"<{key}>", "dataType": "string"} for key in referenced
    ]
    if result is not None:
        settings["result"] = dict(result)
    return McpExecuteTool(id=name, type="database", config=settings, input=referenced)


def _summary(value: Any) -> tuple[DatasetSummary, list[dict[str, Any]]] | None:
    """The summary the MCP server returns with a cached result (``result.preview``), when complete."""
    if not isinstance(value, Mapping):
        return None
    size, count = value.get("sizeBytes"), value.get("rowCount")
    if not isinstance(size, int) or not isinstance(count, int):
        return None
    columns = tuple(str(c) for c in value.get("columns") or [])
    sha = value.get("sha256")
    summary = DatasetSummary(
        row_count=count, columns=columns, size_bytes=size, sha256=sha if isinstance(sha, str) else None
    )
    return summary, rows_of(value.get("rows"))


class McpExecuteKind(_Kind):
    async def execute(self, ctx: NodeContext, node_input: NodeInput) -> NodeOutcome:
        node_id = self.spec.node_id
        mcp = ctx.run.ports.mcp
        if mcp is None:
            raise ZonicError(UNAVAILABLE, node_id=node_id, reason="no MCP server is configured")
        settings = ctx.services.resources.settings
        io = settings.io
        datasets = ctx.run.ports.datasets
        store = datasets.port.store() if datasets is not None else "database"
        cached = store == "cache"
        query = node_input.get(_QUERY)
        tool = db_query_tool(
            self.spec.name,
            self.spec.config,
            query if isinstance(query, str) else "" if query is None else str(query),
            mapping_record(node_input.get(_PARAMETERS)),
            {
                "saveToCache": True,
                # The server requires a binding; the node ignores the flow update and keeps the reference.
                "saveToFlowKey": f"flow.__dataset_{node_id}",
                "ttlSeconds": settings.datasets.cache_ttl_seconds,
                "cacheKeyConfig": {
                    "digest": [{"value": f"{mcp.run_id}:{node_id}:{ctx.visit}", "source": "fixed"}]
                },
                # The server returns the first rows, the count, the columns and the size with the key.
                "preview": {"rows": io.inline_rows},
            }
            if cached
            else None,
        )
        scope = ctx.template_context().scope
        result = await mcp.execute(
            f"{node_id}:{ctx.visit}", [tool], flow=dict(scope.flow), system=dict(scope.system)
        )
        own = result.results.get(tool.id)
        if own is None or own.status == "error":
            reason = own.error if own is not None and own.error else "the query returned no result"
            raise ZonicError(FAILED, node_id=node_id, reason=reason[:500])
        output: Mapping[str, Any] = own.output if isinstance(own.output, Mapping) else {}
        found_data = output.get("data")
        data: Mapping[str, Any] = found_data if isinstance(found_data, Mapping) else {}
        key = data.get("value")
        ref: DatasetRef | None = None
        if datasets is None:
            rows = rows_of(key)
        elif output.get("is_cached") is True and isinstance(key, str):
            owner = datasets.owner(node_id, ctx.visit)
            found = _summary(data.get("preview"))
            # A cached result too large for templates is referenced from the server's summary, never
            # read (design 8.2); a smaller one is read back so later templates see every row.
            if found is not None and (found[0].size_bytes or 0) > io.max_resolved_bytes:
                summary, first = found
                ref = datasets.port.describe(owner, "rows", key, summary)
                return self._outcome(ctx, first, summary.row_count or 0, list(summary.columns), ref)
            ref, value = await datasets.port.adopt(owner, "rows", key)
            rows = rows_of(value)
        else:
            rows = rows_of(key)
            ref = await datasets.port.keep(datasets.owner(node_id, ctx.visit), "rows", rows, store)
        if datasets is not None and held_within(rows, io.max_resolved_bytes):
            datasets.pending.held[node_id] = {"rows": rows}
        return self._outcome(ctx, rows, len(rows), columns_of(rows, io.column_sample_rows), ref)

    def _outcome(
        self,
        ctx: NodeContext,
        rows: list[dict[str, Any]],
        count: int,
        columns: list[str],
        ref: DatasetRef | None,
    ) -> NodeOutcome:
        io = ctx.services.resources.settings.io
        output: dict[str, Any] = {
            "rows": preview(rows, io.inline_rows, io.inline_bytes),
            "rowCount": count,
            "columns": columns,
        }
        if ref is not None:
            output["datasetRef"] = ref.wire()
        return NodeOutcome(
            output=output, activate=self.default(ctx), datasets=[ref] if ref is not None else []
        )
