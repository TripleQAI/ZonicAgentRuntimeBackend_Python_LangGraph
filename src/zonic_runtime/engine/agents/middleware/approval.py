"""``ToolApprovalMiddleware``: tool calls a person approves first (design 9.4 step 2; plan P4-03;
case H-06).

LangChain's ``HumanInTheLoopMiddleware`` in ``per_call`` mode, for the node's tools that
``settings.tools.requireApproval`` names: each such call pauses the run with one interrupt as it
starts, and resumes with the decision, validated by LangGraph against the interrupt's schema:

* ``approve``: the tool runs;
* ``edit``: it runs with the reviewer's arguments (same tool, its own argument types);
* ``reject``: it does not run; the model reads that it was rejected, and why.

The interrupt's value is LangChain's ``ToolApprovalRequest`` with what the pause saves: the kind
``tool_approval``, the node, the prompt (the description, the tool and its arguments) and the
expiry. It is the outermost middleware, so nothing with a side effect runs before the review.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain.agents.middleware.human_in_the_loop import InterruptOnConfig, ToolApprovalRequest

from zonic_runtime.engine.approvals import TOOL_KEY
from zonic_runtime.models.blueprint import NodeSpec

KIND = "tool_approval"
DECISIONS: tuple[Any, ...] = ("approve", "edit", "reject")


class ToolApprovalMiddleware(HumanInTheLoopMiddleware[Any, Any, Any]):
    def __init__(self, spec: NodeSpec, tools: Sequence[str], expiry_hours: float) -> None:
        configs = {name: InterruptOnConfig(allowed_decisions=list(DECISIONS)) for name in tools}
        super().__init__(cast(Any, configs), interrupt_mode="per_call")
        self.spec = spec
        self._hours = expiry_hours

    def _tool_approval_request(self, request: Any, config: InterruptOnConfig) -> ToolApprovalRequest:
        found = super()._tool_approval_request(request, config)
        prompt = {"text": found["description"], TOOL_KEY: found["name"], "args": found["args"]}
        expires = datetime.now(UTC) + timedelta(hours=self._hours)
        extra = {
            "kind": KIND,
            "nodeId": self.spec.node_id,
            "prompt": prompt,
            "expiresAt": expires.isoformat(),
        }
        return cast(ToolApprovalRequest, {**found, **extra})
