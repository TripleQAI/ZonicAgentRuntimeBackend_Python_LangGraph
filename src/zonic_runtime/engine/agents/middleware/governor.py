"""``GovernorMiddleware`` and ``ToolPermitMiddleware`` (design 8.5, 11.2; cases Q-07, Q-08).

* Every model call of an agent loop takes a permit of its model's governor, with the
  conversation's approximate token count against the token bucket.
* Every tool call gets its idempotency key ``nodeId:visit:callIndex`` (DR-2) in issue order, and
  takes one of the visit's tool permits (``tools.maxConcurrentPerTurn``), so an
  agent that asks for ten tools at once runs at most that many together. The permits belong to
  the run (``RunContext.permits``), never to the agent graph, which every run shares.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse, ToolCallRequest
from langchain_core.messages import ToolMessage
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.types import Command

from zonic_runtime.engine.agents.governor import ModelGovernor
from zonic_runtime.models.blueprint import NodeSpec


class GovernorMiddleware(AgentMiddleware[Any, Any]):
    def __init__(self, governor: ModelGovernor) -> None:
        super().__init__()
        self.governor = governor

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        async with self.governor.permit(count_tokens_approximately(request.messages)):
            return await handler(request)


class ToolPermitMiddleware(AgentMiddleware[Any, Any]):
    def __init__(self, spec: NodeSpec) -> None:
        super().__init__()
        self.spec = spec

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        context = request.runtime.context
        permits = getattr(context, "permits", None)
        calls = getattr(context, "tool_calls", None)
        if calls is not None:  # the call's idempotency key, in issue order (DR-2, T-09)
            state = request.state if isinstance(request.state, dict) else {}
            calls.assign(
                self.spec.node_id, int(state.get("zonic_visit") or 0), str(request.tool_call.get("id") or "")
            )
        if permits is None:
            return await handler(request)
        async with permits.tool(self.spec.node_id):
            return await handler(request)
