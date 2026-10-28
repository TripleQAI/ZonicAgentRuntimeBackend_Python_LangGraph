"""``HandoffMiddleware``: ends the agent loop once a handoff was chosen (design 8.5)."""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain.agents.middleware.types import JumpTo
from langgraph.runtime import Runtime

from zonic_runtime.engine.agents.state import ZonicAgentState

_END: JumpTo = "end"


class HandoffMiddleware(AgentMiddleware[ZonicAgentState, Any]):
    state_schema = ZonicAgentState

    @hook_config(can_jump_to=[_END])
    def before_model(self, state: ZonicAgentState, runtime: Runtime[Any]) -> dict[str, Any] | None:
        return {"jump_to": _END} if state.get("zonic_handoff") else None

    @hook_config(can_jump_to=[_END])
    async def abefore_model(self, state: ZonicAgentState, runtime: Runtime[Any]) -> dict[str, Any] | None:
        return self.before_model(state, runtime)
