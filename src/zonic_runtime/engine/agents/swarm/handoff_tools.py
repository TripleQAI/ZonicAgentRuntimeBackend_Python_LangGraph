"""The Swarm's handoff tools and limits (design 8.6; plan P4-01, Section 15.13; cases W-02, W-04, W-06).

Each member gets ``transfer_to_<member>(message, context)`` for every other member. A call first
checks the limits over the member's step (``ZonicAgentState.zonic_swarm``):

* the step limit ``min(maxHandoffs + 1, maxIterations)``: a handoff past it is refused and the
  Swarm fails with ``SWARM_STEP_LIMIT``;
* repetitive handoff detection: fewer than ``unique`` different members among the last
  ``window`` member steps is ``SWARM_REPETITIVE_HANDOFF``.

It returns ``Command(goto=<member>, graph=Command.PARENT)``, which routes the Swarm graph from
inside the member's agent graph (SP-6), with the paired handoff messages: the model's
``AIMessage`` with this call only, and a ``ToolMessage`` acknowledging it, so the shared history
never holds a dangling call. The step's own messages and flow changes ride along under
``MEMBER_MESSAGES`` and ``MEMBER_FLOW``; the member node records the step from them and removes
them before the Swarm graph applies the update.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.prebuilt import ToolRuntime
from langgraph.types import Command
from pydantic import Field, create_model

from zonic_runtime.errors import ZonicError
from zonic_runtime.models.nodes.configs import MultiAgentNodeConfig
from zonic_runtime.models.resources.settings import SwarmSettings

TRANSFER_PREFIX = "transfer_to_"
MEMBER_MESSAGES, MEMBER_FLOW = "zonic_member_messages", "zonic_member_flow"
STEP_LIMIT, REPETITIVE = "SWARM_STEP_LIMIT", "SWARM_REPETITIVE_HANDOFF"


@dataclass(frozen=True)
class SwarmMemberRef:
    """A member as the others see it: its name (the node name, unique by rule SW-3), its Swarm
    graph node and its description."""

    node_id: str
    name: str
    graph_name: str
    description: str = ""


@dataclass(frozen=True)
class SwarmLimits:
    #: Member steps a visit may take: ``min(maxHandoffs + 1, maxIterations)``.
    steps: int
    #: Repetitive handoff detection over the last ``window`` steps; 0 is off.
    window: int = 0
    unique: int = 0

    @classmethod
    def of(
        cls, config: MultiAgentNodeConfig, settings: SwarmSettings, members: int
    ) -> tuple[SwarmLimits, bool]:
        """The limits, and whether detection was asked for but is off: both settings are needed,
        and the minimum must fit the window and the members (rule SW-11; case W-06)."""
        handoffs = config.max_handoffs if config.max_handoffs is not None else settings.max_handoffs
        iterations = config.max_iterations if config.max_iterations is not None else settings.max_iterations
        window = config.repetitive_handoff_detection_window or 0
        unique = config.repetitive_handoff_min_unique_agents or 0
        detect = window > 0 and 0 < unique <= min(window, members)
        steps = max(1, min(handoffs + 1, iterations))
        limits = cls(steps, window, unique) if detect else cls(steps)
        return limits, not detect and (window > 0 or unique > 0)

    def check(self, swarm: Mapping[str, Any], target: str, node_id: str) -> None:
        member = str(swarm.get("member") or "")
        step = int(swarm.get("step") or 1)
        if step + 1 > self.steps:
            raise ZonicError(STEP_LIMIT, node_id=node_id, member=member, target=target, limit=self.steps)
        if self.window:
            recent = [*(swarm.get("history") or []), member][-self.window :]
            if len(recent) >= self.window and len(set(recent)) < self.unique:
                raise ZonicError(
                    REPETITIVE,
                    node_id=node_id,
                    member=member,
                    target=target,
                    window=self.window,
                    unique=len(set(recent)),
                )


def _paired(messages: Sequence[BaseMessage], call_id: str) -> AIMessage:
    """The model's message that made this call, with only this call: another call of the same
    turn (a second handoff, a tool whose result the Swarm never sees) would dangle."""
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            calls = [c for c in message.tool_calls if c.get("id") == call_id]
            if calls:
                return AIMessage(content=message.text, tool_calls=calls, id=message.id, name=message.name)
    raise ZonicError("INTERNAL", reason="handoff call not found in the member's messages")


def transfer_tool(
    target: SwarmMemberRef, limits: SwarmLimits, node_id: str, acknowledgement: str
) -> BaseTool:
    """``transfer_to_<target>``; ``node_id`` is the Swarm node's, for the limit errors."""
    name = f"{TRANSFER_PREFIX}{target.name}"
    arguments = create_model(
        "TransferArguments",
        message=(str, Field(description="What the next member should do, and what you found.")),
        context=(dict[str, Any] | None, Field(default=None, description="Facts the next member needs.")),
    )

    # ``runtime`` is injected by the tool node; a plain ``ToolRuntime`` annotation is what it detects.
    async def transfer(
        message: str,
        context: dict[str, Any] | None = None,
        runtime: ToolRuntime = None,  # type: ignore[assignment]
    ) -> Command[Any]:
        swarm = runtime.state.get("zonic_swarm") or {}
        limits.check(swarm, target.name, node_id)
        messages = list(runtime.state["messages"])
        call_id = runtime.tool_call_id or ""
        entry = {"from": swarm.get("member"), "to": target.name, "message": message, "context": context or {}}
        ack = ToolMessage(acknowledgement.replace("{member}", target.name), tool_call_id=call_id, name=name)
        return Command(
            goto=target.graph_name,
            graph=Command.PARENT,
            update={
                "active_member": target.name,
                "step": int(swarm.get("step") or 1) + 1,
                "handoffs": [entry],
                "messages": [_paired(messages, call_id), ack],
                MEMBER_MESSAGES: messages[int(swarm.get("inputs") or 0) :],
                MEMBER_FLOW: dict(runtime.state.get("zonic_flow_updates") or {}),
            },
        )

    description = f"Hand the task to {target.name}" + (
        f": {target.description}" if target.description else "."
    )
    return StructuredTool.from_function(
        coroutine=transfer, name=name, description=description, args_schema=arguments
    )
