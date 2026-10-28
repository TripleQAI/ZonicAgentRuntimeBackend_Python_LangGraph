"""``ToolFactory``: a node's configured tools as LangChain tools (design 7.6, 7.8; plan P3-02;
cases N-05 tools part, T-03 to T-05, T-07, T-09), after the Node.js runtime's.

* ``database``, ``api``, ``function`` (the builder's MCP configured tools): tools that call the MCP
  ``execute`` tool with the configured tool (secrets resolved at call time), the model's input,
  and the visit's ``flow`` and ``system`` as context. The reply's ``flow`` updates go back to the
  run (``zonic_flow_updates``); a per-tool error is a tool error the model sees (T-02).
* ``mcp``, ``agentcore_gateway`` (a server's own tool): tools with the document's name,
  description and schema that reach the server only when called, so a server that is down is a
  tool error for the model, not a failed node.
* ``strands_builtin``: the runtime's own tools on ``tools.builtinAllowList``; another name is
  ``TOOL_NOT_ALLOWED`` (T-05). Shell, file and REPL tools are not implemented at all.

Every tool checks the model's input against its schema first; an invalid input is an error result
the model can correct (T-03). Tools are built once per node and hold no run data: a call reads the
run only through ``ToolRuntime`` (``context.ports``, the agent state), and its MCP request ID is
the call's idempotency key ``runId:nodeId:visit:callIndex`` (DR-2, T-09), which
``ToolPermitMiddleware`` assigns in issue order.
"""

from __future__ import annotations

import json
import math
import operator
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jsonschema import Draft202012Validator
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, StructuredTool, ToolException
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command

from zonic_runtime.engine.context import RunContext
from zonic_runtime.errors import ZonicError
from zonic_runtime.models.blueprint import NodeSpec
from zonic_runtime.models.dialects.refs import ToolRef
from zonic_runtime.models.mcp import McpExecuteTool
from zonic_runtime.models.resources.settings import ToolSettings
from zonic_runtime.ports.secrets import resolve_secrets

_MAX_NAME = 64
_UNNAMED = "unnamed_tool"
#: The key of an ``output.data`` binding naming its ``flow`` variable (the MCP server's field).
_VARIABLE = "variable"  # principle: allow rule-1-node-type mcp-reply-field
_RUNTIME_SOURCES = frozenset({"flow", "system", "fixed", "static", "constant", "context"})
_JSON_TYPES = MappingProxyType(
    {
        "string": "string",
        "number": "number",
        "integer": "integer",
        "boolean": "boolean",
        "date": "string",
        "datetime": "string",
        "array": "array",
        "object": "object",
    }
)
_EXECUTE_TYPES = frozenset({"database", "api", "function"})
_SERVER_TYPES = frozenset({"mcp", "agentcore_gateway"})
_RESOURCE = "mcp-resource"
_BUILTIN = "strands_builtin"
_EMPTY_SCHEMA: Mapping[str, Any] = MappingProxyType({"type": "object", "properties": {}})


def tool_name(name: str) -> str:
    """Names model providers accept: letters, digits, ``_`` and ``-``, at most 64 characters."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)[:_MAX_NAME] or _UNNAMED


def run_context(runtime: ToolRuntime) -> RunContext:
    """The run a tool call belongs to: the only way a tool reads the run (T-08)."""
    found = runtime.context
    if not isinstance(found, RunContext):
        raise ToolException("the tool was called outside a run")
    return found


def _fail(message: str) -> ToolException:
    return ToolException(message)


def _validated(schema: Mapping[str, Any], name: str, given: Mapping[str, Any]) -> dict[str, Any]:
    errors = sorted(Draft202012Validator(dict(schema)).iter_errors(dict(given)), key=lambda e: list(e.path))
    if errors:
        detail = "; ".join(f"/{'/'.join(str(p) for p in e.path)} {e.message}" for e in errors[:5])
        raise _fail(f"invalid input for {name}: {detail}")  # the model sees it and may correct it (T-03)
    return dict(given)


def input_schema(ref: ToolRef, tool_type: str) -> dict[str, Any]:
    """The model's part of a configured tool: its parameters no runtime source fills."""
    if tool_type == "function" and ref.settings.get("payloadType") == "from_llm":
        return {
            "type": "object",
            "additionalProperties": True,
            "description": "The payload for the function.",
        }
    parameters = [p for p in ref.settings.get("parameters") or [] if isinstance(p, Mapping)]
    own = [
        p
        for p in parameters
        if isinstance(p.get("name"), str) and str(p.get("source") or "flow") not in _RUNTIME_SOURCES
    ]
    properties = {}
    for parameter in own:
        entry: dict[str, Any] = {
            "type": _JSON_TYPES.get(str(parameter.get("dataType") or "string"), "string")
        }
        if isinstance(parameter.get("description"), str) and parameter["description"]:
            entry["description"] = parameter["description"]
        properties[parameter["name"]] = entry
    return {
        "type": "object",
        "properties": properties,
        "required": [p["name"] for p in own if p.get("required") is True],
    }


def _content(result: Any) -> Any:
    """An MCP tool result as the model reads it: structured content, else the text parts."""
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return structured
    texts = [
        getattr(p, "text", "")
        for p in getattr(result, "content", None) or []
        if getattr(p, "type", "") == "text"
    ]
    return "\n".join(texts)


# -- built-ins ----------------------------------------------------------------------------------

_OPERATORS: Mapping[str, Callable[[float, float], float]] = MappingProxyType(
    {
        "+": operator.add,
        "-": operator.sub,
        "*": operator.mul,
        "/": operator.truediv,
        "%": operator.mod,
    }
)
_TOKEN = re.compile(r"\d+(?:\.\d+)?(?:e[+-]?\d+)?|[-+*/%^()]|\S", re.IGNORECASE)


def calculate(expression: str) -> float:
    """Arithmetic without ``eval``: numbers, ``+ - * / % ^``, parentheses and unary minus."""
    tokens = _TOKEN.findall(expression)
    position = 0

    def peek() -> str | None:
        return tokens[position] if position < len(tokens) else None

    def take() -> str | None:
        nonlocal position
        token = peek()
        position += 1
        return token

    def fail() -> float:
        raise _fail(f'cannot evaluate "{expression}"')

    def primary() -> float:
        token = take()
        if token == "(":
            value = total()
            return value if take() == ")" else fail()
        if token == "-":
            return -primary()
        if token == "+":
            return primary()
        if token is None or not token[0].isdigit():
            return fail()
        return float(token)

    def power() -> float:
        base = primary()
        if peek() != "^":
            return base
        take()
        return float(base ** power())

    def product() -> float:
        value = power()
        while peek() in ("*", "/", "%"):
            symbol = take() or ""
            right = power()
            if right == 0 and symbol in ("/", "%"):
                return fail()
            value = _OPERATORS[symbol](value, right)
        return value

    def total() -> float:
        value = product()
        while peek() in ("+", "-"):
            symbol = take() or ""
            value = _OPERATORS[symbol](value, product())
        return value

    value = total()
    if position != len(tokens) or not math.isfinite(value):
        fail()
    return int(value) if value.is_integer() else value


async def _calculator(expression: str) -> dict[str, Any]:
    return {"result": calculate(expression)}


async def _current_time(timezone: str | None = None) -> dict[str, Any]:
    now = datetime.now(UTC)
    found: dict[str, Any] = {"utc": now.isoformat(timespec="seconds").replace("+00:00", "Z")}
    if timezone:
        try:
            found |= {
                "local": now.astimezone(ZoneInfo(timezone)).isoformat(timespec="seconds"),
                "timezone": timezone,
            }
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise _fail(f"unknown time zone {timezone}") from exc
    return found


_BUILTINS: Mapping[str, tuple[str, dict[str, Any], Callable[..., Any]]] = MappingProxyType(
    {
        "calculator": (
            "Evaluates an arithmetic expression with + - * / % ^ and parentheses.",
            {
                "type": "object",
                "properties": {"expression": {"type": "string", "description": "For example (3 + 4) * 2"}},
                "required": ["expression"],
            },
            _calculator,
        ),
        "current_time": (
            "The current date and time (ISO 8601, UTC, and in an optional IANA time zone).",
            {
                "type": "object",
                "properties": {"timezone": {"type": "string", "description": "For example Europe/Oslo"}},
            },
            _current_time,
        ),
    }
)


# -- the factory ----------------------------------------------------------------------------------


class ToolFactory:
    """Implements ``ToolProvider``."""

    def __init__(self, settings: ToolSettings) -> None:
        self._settings = settings

    async def tools(self, spec: NodeSpec) -> Sequence[BaseTool]:
        refs = [ToolRef.model_validate(t) for t in getattr(spec.config, "tools", None) or []]
        return [self.create(spec, ref) for ref in refs if ref.enabled]

    def create(self, spec: NodeSpec, ref: ToolRef) -> BaseTool:
        if ref.tool_type in _EXECUTE_TYPES:
            return self._execute_tool(spec, ref)
        if ref.tool_type in _SERVER_TYPES:
            return self._server_tool(spec, ref)
        if ref.tool_type == _BUILTIN:
            return self._builtin(spec, ref)
        if ref.tool_type == _RESOURCE:
            return self._resource_tool(ref)
        raise ZonicError(
            "TOOL_UNAVAILABLE",
            node_id=spec.node_id,
            tool=ref.name,
            reason=f"tool type {ref.tool_type} is not supported",
        )

    def _tool(self, ref: ToolRef, schema: Mapping[str, Any], call: Callable[..., Any]) -> BaseTool:
        alias = ref.settings.get("alias")
        name = tool_name(alias if isinstance(alias, str) and alias else ref.name or ref.tool_type)
        checked = dict(schema)

        async def run(runtime: ToolRuntime, **given: Any) -> Any:
            return await call(runtime, _validated(checked, name, given))

        return StructuredTool(
            name=name,
            description=ref.description or ref.title or name,
            args_schema=checked,
            coroutine=run,
            handle_tool_error=True,
        )

    def _execute_tool(self, spec: NodeSpec, ref: ToolRef) -> BaseTool:
        tool_type = ref.tool_type
        settings = {k: v for k, v in ref.settings.items() if k != "server"}
        schema = ref.schema_ or input_schema(ref, tool_type)
        tool_id = ref.id or ref.name or tool_type

        async def call(runtime: ToolRuntime, given: dict[str, Any]) -> Any:
            context = run_context(runtime)
            mcp = context.ports.mcp
            if mcp is None:
                raise _fail(f"{ref.name}: the MCP server is not configured")
            secrets = context.ports.secrets
            try:
                config = resolve_secrets(
                    {"name": ref.name, **settings}, secrets.get if secrets is not None else (lambda _: None)
                )
                key = context.tool_calls.key(runtime.tool_call_id or "") or f"{spec.node_id}:0:{tool_id}"
                state = runtime.state if isinstance(runtime.state, Mapping) else {}
                result = await mcp.execute(
                    key,
                    [
                        McpExecuteTool.model_validate(
                            {"id": tool_id, "type": tool_type, "config": config, "input": given}
                        )
                    ],
                    flow=state.get("zonic_flow") or {},
                    system=state.get("zonic_system") or {},
                )
            except ZonicError as error:
                raise _fail(f"{ref.name}: {error.code} {error.details.get('reason', '')}".strip()) from error
            own = result.results.get(tool_id)
            reply: Any
            if own is None or own.status == "error":
                reply = None
                failure = own.error if own is not None else "the tool returned no result"
            else:
                failure = None
                output = own.output
                data = output.get("data") if isinstance(output, Mapping) else None
                if (
                    isinstance(output, Mapping)
                    and output.get("is_cached") is True
                    and isinstance(data, Mapping)
                ):
                    reply = {"saved": True, _VARIABLE: data.get(_VARIABLE), "key": data.get("value")}
                    # A cached result is a dataset of this turn (design 8.2): saved with the turn.
                    datasets = context.ports.datasets
                    if datasets is not None and isinstance(data.get("value"), str):
                        visit = state.get("zonic_visit")
                        datasets.port.register(
                            datasets.owner(spec.node_id, visit if isinstance(visit, int) else 1),
                            "rows" if tool_type == "database" else "tool_result",
                            data["value"],
                        )
                else:
                    reply = data.get("value") if isinstance(data, Mapping) else output
            updates = dict(result.flow_updates)
            text = (
                json.dumps(reply, ensure_ascii=False, default=str) if failure is None else f"error: {failure}"
            )
            message = ToolMessage(
                content=text,
                tool_call_id=runtime.tool_call_id or "",
                name=self_name(ref),
                status="error" if failure is not None else "success",
            )
            return Command(
                update={"messages": [message], "zonic_flow_updates": updates, "zonic_flow": updates}
            )

        return self._tool(ref, schema, call)

    def _server_tool(self, spec: NodeSpec, ref: ToolRef) -> BaseTool:
        defaults = (
            ref.settings.get("parameters") if isinstance(ref.settings.get("parameters"), Mapping) else {}
        )

        async def call(runtime: ToolRuntime, given: dict[str, Any]) -> Any:
            mcp = run_context(runtime).ports.mcp
            if mcp is None:
                raise _fail(f"{ref.name}: the MCP server is not configured")
            try:
                # The MCP name of the tool; the platform's ``toolId`` is its own ID, not the protocol name.
                result = await mcp.port.call(
                    ref.server_id, ref.name or ref.tool_id or "", {**dict(defaults or {}), **given}
                )
            except ZonicError as error:
                raise _fail(f"{ref.name}: {error.code} {error.details.get('reason', '')}".strip()) from error
            if getattr(result, "is_error", False):
                raise _fail(str(_content(result)))
            return _content(result)

        return self._tool(ref, ref.schema_ or _EMPTY_SCHEMA, call)

    def _resource_tool(self, ref: ToolRef) -> BaseTool:
        """A server's resource (platform ``mcp-resource``, a reference file), read when the model
        asks; it takes no arguments."""
        inner = ref.settings.get("settings")
        uri = inner.get("uri") if isinstance(inner, Mapping) and isinstance(inner.get("uri"), str) else None
        target = uri or ref.tool_id or ""

        async def call(runtime: ToolRuntime, _: dict[str, Any]) -> Any:
            mcp = run_context(runtime).ports.mcp
            if mcp is None:
                raise _fail(f"{ref.name}: the MCP server is not configured")
            try:
                return await mcp.port.resource(ref.server_id, target)
            except ZonicError as error:
                raise _fail(f"{ref.name}: {error.code} {error.details.get('reason', '')}".strip()) from error

        return self._tool(ref, _EMPTY_SCHEMA, call)

    def _builtin(self, spec: NodeSpec, ref: ToolRef) -> BaseTool:
        found = _BUILTINS.get(ref.name)
        if found is None or ref.name not in self._settings.builtin_allow_list:
            raise ZonicError("TOOL_NOT_ALLOWED", node_id=spec.node_id, tool=ref.name)
        description, schema, function = found

        async def call(_: ToolRuntime, given: dict[str, Any]) -> Any:
            return await function(**given)

        named = ToolRef(
            tool_type=ref.tool_type, name=ref.name, description=description, settings=ref.settings
        )
        return self._tool(named, schema, call)


def self_name(ref: ToolRef) -> str:
    alias = ref.settings.get("alias")
    return tool_name(alias if isinstance(alias, str) and alias else ref.name or ref.tool_type)
