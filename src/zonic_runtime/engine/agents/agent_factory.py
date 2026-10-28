"""``AgentFactory`` and the agent services of the node template (design 8.5; plan P2-02).

One ``create_agent`` graph per node spec, built on the node's first visit and kept by its kind,
which lives as long as the compiled graph (so the graph cache holds it). The system prompt and
history are not part of the graph: the kind passes them as the first messages of each visit,
since they are templates resolved per visit.

The middleware stack (outermost first): tool approvals (the node's tools that
``settings.tools.requireApproval`` names, P4-03), hooks, the model's governor, tool permits, the
iteration limit, then the handoff check.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from zonic_runtime.config.env import EnvironmentModel
from zonic_runtime.config.settings import RuntimeResources
from zonic_runtime.engine.agents.governor import ModelGovernor
from zonic_runtime.engine.agents.middleware import HandoffMiddleware, TurnLimitMiddleware
from zonic_runtime.engine.agents.middleware.approval import ToolApprovalMiddleware
from zonic_runtime.engine.agents.middleware.governor import GovernorMiddleware, ToolPermitMiddleware
from zonic_runtime.engine.agents.middleware.hooks import HookMiddleware
from zonic_runtime.engine.agents.model_factory import (
    BedrockClient,
    ModelBuilder,
    ModelFactory,
    provider_builders,
)
from zonic_runtime.engine.agents.state import ZonicAgentState
from zonic_runtime.engine.agents.tool_factory import ToolFactory
from zonic_runtime.engine.context import RunContext
from zonic_runtime.errors import ZonicError
from zonic_runtime.models.blueprint import NodeSpec
from zonic_runtime.models.dialects.refs import ToolRef


class ToolProvider(Protocol):
    """The tools a node's configuration names (MCP, builder ``database``/``api``/``function``
    rows, built-ins); ``ToolFactory`` implements it in P3-02."""

    async def tools(self, spec: NodeSpec) -> Sequence[BaseTool]: ...

    def create(self, spec: NodeSpec, ref: ToolRef) -> BaseTool:
        """One tool, for a ``tool`` node that calls it without a model (P3-06)."""
        ...


class NoTools:
    """Nodes run without their configured tools (tests that want none)."""

    def create(self, spec: NodeSpec, ref: ToolRef) -> BaseTool:
        raise ZonicError("TOOL_UNAVAILABLE", node_id=spec.node_id, tool=ref.name, reason="no tools")

    async def tools(self, spec: NodeSpec) -> Sequence[BaseTool]:
        return []


class AgentFactory:
    def __init__(self, require_approval: Sequence[str] = (), expiry_hours: float = 0.0) -> None:
        self._require = frozenset(require_approval)
        self._expiry_hours = expiry_hours

    def build(
        self,
        *,
        spec: NodeSpec,
        model: BaseChatModel,
        tools: Sequence[BaseTool],
        response_format: Any = None,
        max_iterations: int | None = None,
        governor: ModelGovernor | None = None,
    ) -> Any:
        middleware: list[AgentMiddleware[Any, Any]] = []
        gated = [t.name for t in tools if t.name in self._require]
        if gated:  # outermost: nothing with a side effect runs before the review (H-06)
            middleware.append(ToolApprovalMiddleware(spec, gated, self._expiry_hours))
        middleware.append(HookMiddleware(spec))
        if governor is not None:
            middleware.append(GovernorMiddleware(governor))
        middleware.append(ToolPermitMiddleware(spec))
        if max_iterations:
            middleware.append(TurnLimitMiddleware(max_iterations))
        middleware.append(HandoffMiddleware())
        return create_agent(
            model,
            tools=list(tools),
            middleware=middleware,
            response_format=response_format,
            state_schema=ZonicAgentState,
            context_schema=RunContext,
            name=spec.graph_name,
        )


@dataclass(frozen=True)
class AgentServices:
    """Process-wide: models, tools and agent graphs for the LLM and agent kinds."""

    models: ModelFactory
    tools: ToolProvider
    factory: AgentFactory

    @classmethod
    def from_resources(
        cls,
        resources: RuntimeResources,
        *,
        builders: Mapping[str, ModelBuilder] | None = None,
        tools: ToolProvider | None = None,
        environment: EnvironmentModel | None = None,
        bedrock: BedrockClient | None = None,
    ) -> AgentServices:
        def read(field: str) -> str | None:
            value = getattr(environment, field, None) if environment is not None else None
            return str(value) if value is not None else None

        found: Mapping[str, ModelBuilder] = builders or provider_builders(read, bedrock)
        models = ModelFactory(resources.models, resources.settings.models, found)
        settings = resources.settings
        factory = AgentFactory(settings.tools.require_approval, settings.interrupts.default_expiry_hours)
        return cls(models=models, tools=tools or ToolFactory(settings.tools), factory=factory)
