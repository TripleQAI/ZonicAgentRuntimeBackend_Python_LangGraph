"""Handoff and routing tools of an agent node (design 8.5; plan P2-02, cases N-07, B-04).

* The builder's ``handoffTarget`` list becomes one ``handoff_to(target, reason)`` tool whose
  ``target`` is an enum of the targets' names. A name outside the enum fails the tool call's
  validation; the model sees the error and may choose again, and when it never does the node
  activates ``default``.
* A platform routing tool keeps its own name and argument schema. A call hands off along its
  edge, and the arguments (with the preset ``parameters`` merged over them) become
  ``toolResults[<name>]`` for the target to read.

Both record the decision in ``ZonicAgentState.zonic_handoff``; ``HandoffMiddleware`` then
ends the loop before the next model call. The tools read the visit only through the agent
state, so they are built once per node and hold no run data.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command
from pydantic import Field, create_model

from zonic_runtime.models.dialects.refs import RoutingToolRef

#: The builder's handoff tool name.
HANDOFF_TOOL = "handoff_to"
_EMPTY_SCHEMA: Mapping[str, Any] = MappingProxyType({"type": "object", "properties": {}})


@dataclass(frozen=True)
class HandoffTarget:
    node_id: str
    name: str


def _decision(runtime: ToolRuntime, decision: dict[str, Any], reply: str, **extra: Any) -> Command[Any]:
    return Command(
        update={
            "zonic_handoff": decision,
            "messages": [ToolMessage(reply, tool_call_id=runtime.tool_call_id or "")],
            **extra,
        }
    )


def handoff_tool(targets: Sequence[HandoffTarget]) -> BaseTool:
    names = tuple(t.name for t in targets)
    by_name = {t.name: t for t in targets}
    target: Any = Literal[names]  # an enum of the targets' names in the tool's JSON Schema
    arguments = create_model(
        "HandoffArguments",
        target=(target, Field(description="The step to hand the conversation to.")),
        reason=(str, Field(default="", description="Why this step.")),
    )

    # ``runtime`` is injected by the tool node; a plain ``ToolRuntime`` annotation is what it detects.
    async def handoff(target: str, reason: str = "", runtime: ToolRuntime = None) -> Command[Any]:  # type: ignore[assignment]
        chosen = by_name[target]
        decision = {
            "target": chosen.node_id,
            "name": chosen.name,
            "via": HANDOFF_TOOL,
            "input": {"target": target, "reason": reason},
        }
        return _decision(runtime, decision, f"Handing off to {chosen.name}.")

    return StructuredTool.from_function(
        coroutine=handoff,
        name=HANDOFF_TOOL,
        description=f"Hand the conversation to the next step. Choose one of: {', '.join(names)}.",
        args_schema=arguments,
    )


def routing_tool(ref: RoutingToolRef, target_name: str) -> BaseTool:
    name = ref.call_name

    async def route(runtime: ToolRuntime = None, **arguments: Any) -> Command[Any]:  # type: ignore[assignment]
        presets = (runtime.state.get("zonic_presets") or {}).get(name) or {}
        merged = {**arguments, **presets}
        routed = {**(runtime.state.get("zonic_routed") or {}), name: merged}
        decision = {"target": ref.target, "name": target_name, "via": name, "input": merged}
        return _decision(runtime, decision, f"Continuing with {target_name}.", zonic_routed=routed)

    return StructuredTool.from_function(
        coroutine=route,
        name=name,
        description=ref.description or ref.title or f"Continue with {target_name}.",
        args_schema=dict(ref.schema_ or _EMPTY_SCHEMA),
        return_direct=False,
    )
