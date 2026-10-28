"""Scripted models and tools for the real LLM and agent kinds (plan P2-02).

``ScriptedModels`` replaces every provider's model builder: it records the bounded arguments
each node's model was built with and hands out one ``ScriptedChatModel``, so one script drives
every model call of a test. A ``nostream`` node gets a copy carrying the tag (its cursor starts
where the shared model's is, so give such a test its own script).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import BaseTool

from tests.fakes.scripted_chat_model import ScriptedChatModel, Turn
from tests.kinds.harness import Harness, Recorder
from zonic_runtime.config.settings import RuntimeResources
from zonic_runtime.engine.agents.agent_factory import AgentServices
from zonic_runtime.engine.agents.model_factory import ModelBuilder
from zonic_runtime.engine.kinds.core import core_kinds
from zonic_runtime.models.blueprint import NodeSpec
from zonic_runtime.models.dialects.refs import ToolRef
from zonic_runtime.models.resources.catalogs import ModelEntry

PROVIDERS = ("bedrock_converse", "azure_openai", "anthropic", "openai")


@dataclass
class ScriptedModels:
    model: ScriptedChatModel
    built: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def builders(self) -> dict[str, ModelBuilder]:
        def build(entry: ModelEntry, arguments: dict[str, Any]) -> Any:
            self.built.append((entry.model_id, dict(arguments)))
            tags = arguments.get("tags")
            return self.model.model_copy(update={"tags": tags}) if tags else self.model

        return dict.fromkeys(PROVIDERS, build)


@dataclass
class FakeTools:
    """The configured tools of each node, by node ID (P3-02's ``ToolFactory`` stand-in)."""

    by_node: dict[str, list[BaseTool]] = field(default_factory=dict)

    async def tools(self, spec: NodeSpec) -> Sequence[BaseTool]:
        return self.by_node.get(spec.node_id, [])

    def create(self, spec: NodeSpec, ref: ToolRef) -> BaseTool:
        return next(t for t in self.by_node.get(spec.node_id, []) if t.name == ref.name)


def agent_harness(
    script: Sequence[Turn],
    resources: RuntimeResources,
    *,
    tools: dict[str, list[BaseTool]] | None = None,
    kinds: dict[str, Any] | None = None,
) -> tuple[Harness, ScriptedModels]:
    models = ScriptedModels(ScriptedChatModel(script=list(script)))
    agents = AgentServices.from_resources(resources, builders=models.builders(), tools=FakeTools(tools or {}))
    recorder = Recorder()
    return Harness(resources, {**core_kinds(), **(kinds or {})}, recorder=recorder, agents=agents), models
