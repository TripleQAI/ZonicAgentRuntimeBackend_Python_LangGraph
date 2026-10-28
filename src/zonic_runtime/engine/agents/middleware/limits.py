"""``TurnLimitMiddleware``: ``maxIterations`` as LangChain's ``ModelCallLimitMiddleware``
(design 8.5, case N-06).

With ``exit_behavior="end"`` the parent ends the loop before a call past ``run_limit`` and adds a
message about the limit. Here the loop ends the same way, but the agent state records
``zonic_limited`` instead of the extra message, so the node's answer is the last real model text
and the kind reports ``AGENT_TURN_LIMIT``.
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import ModelCallLimitMiddleware, hook_config
from langchain.agents.middleware.types import JumpTo
from langgraph.runtime import Runtime

_END: JumpTo = "end"


class TurnLimitMiddleware(ModelCallLimitMiddleware):
    def __init__(self, run_limit: int) -> None:
        super().__init__(run_limit=run_limit, exit_behavior="end")

    @staticmethod
    def _limited(update: dict[str, Any] | None) -> dict[str, Any] | None:
        if update is None:
            return None
        return {"jump_to": update.get("jump_to", _END), "zonic_limited": True}

    @hook_config(can_jump_to=[_END])
    def before_model(self, state: Any, runtime: Runtime[Any]) -> dict[str, Any] | None:
        return self._limited(super().before_model(state, runtime))

    @hook_config(can_jump_to=[_END])
    async def abefore_model(self, state: Any, runtime: Runtime[Any]) -> dict[str, Any] | None:
        return self._limited(await super().abefore_model(state, runtime))
