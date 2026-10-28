"""``WorkflowRuntime``: from a workflow ID to its compiled graph (design 9.1 steps 1 and 2).

Loader (catalog visibility, document cache), blueprint cache and graph cache, composed once per
process. ``prepare`` costs one catalog read when the workflow's revision is cached, else the
document read, the plan and the compile (case D-09).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

from zonic_runtime.application.caller import Caller
from zonic_runtime.application.subflows import SubflowOpener
from zonic_runtime.config.settings import RuntimeResources
from zonic_runtime.engine.agents.agent_factory import AgentServices
from zonic_runtime.engine.agents.policy_guard import PolicyGuard
from zonic_runtime.engine.error_policy import error_handler
from zonic_runtime.engine.kinds.core import core_registry
from zonic_runtime.engine.kinds.registry import KindRegistry
from zonic_runtime.engine.node_template import NodeServices, ServicePorts, runner_factory
from zonic_runtime.engine.persistence.turn_saver import TurnSaver, serializer
from zonic_runtime.hooks.builtin import builtin_hooks
from zonic_runtime.hooks.integration import HookPorts, integration_hooks
from zonic_runtime.hooks.node_hooks import TemplateHooks
from zonic_runtime.hooks.registry import HookRegistry
from zonic_runtime.hooks.resolver import HookResolver
from zonic_runtime.hooks.runner import HookRunner
from zonic_runtime.hooks.types import NodeHook
from zonic_runtime.models.blueprint import Blueprint
from zonic_runtime.ports.rows import RowReader
from zonic_runtime.workflow.blueprint.builder import BlueprintBuilder, BlueprintCache
from zonic_runtime.workflow.compiler.graph_cache import GraphCache
from zonic_runtime.workflow.compiler.workflow_compiler import WorkflowCompiler
from zonic_runtime.workflow.loader.loader import LoadedDocument, WorkflowLoader


@dataclass(frozen=True)
class Prepared:
    loaded: LoadedDocument
    blueprint: Blueprint
    graph: Any


class WorkflowRuntime:
    def __init__(
        self,
        resources: RuntimeResources,
        rows: RowReader,
        *,
        agents: AgentServices | None = None,
        kinds: KindRegistry | None = None,
        saver: TurnSaver | None = None,
        hook_types: Sequence[NodeHook] = (),
        aes_key: bytes | None = None,
        ports: ServicePorts | None = None,
    ) -> None:
        caches = resources.settings.caches
        self.resources = resources
        self.loader = WorkflowLoader.from_resources(resources, rows)
        self.blueprints = BlueprintCache(BlueprintBuilder(resources).build, caches.blueprints)
        #: Sub-flows open their children through this runtime's caches (P3-09); bound below.
        self.subflows = SubflowOpener(resources.settings.catalog.cache_seconds, caches.documents)
        ports = replace(ports or ServicePorts(), subflows=self.subflows)
        self.services = NodeServices.from_resources(resources, agents=agents, ports=ports)
        settings = resources.settings
        hook_ports = HookPorts(
            hooks=settings.hooks,
            guardrails=settings.guardrails,
            egress=ports.egress,
            secrets=ports.secrets,
            mcp=ports.mcp,
            cache=ports.cache,
            guard=PolicyGuard(settings.guardrails, self.services.agents.models)
            if self.services.agents is not None
            else None,
            managed_guard=ports.guardrails,
        )
        registry = HookRegistry(
            [*builtin_hooks(self.services.schemas), *integration_hooks(hook_ports), *hook_types]
        )
        resolver = HookResolver(registry, resources.hooks, resources.settings.hooks)
        #: The run's hook points (``RunContext.hooks``): template, driver and agent middleware.
        self.hooks = TemplateHooks(HookRunner(resolver, resources.settings.hooks), self.services)
        #: The root graphs' checkpointer (design 9.2): one buffer per run, attached and detached by
        #: the run, never a gateway call.
        state = resources.settings.state
        self.saver: TurnSaver = (
            saver
            if saver is not None
            else TurnSaver(serde=serializer(aes_key), keep=state.checkpoints_kept_per_namespace)
        )
        compiler = WorkflowCompiler(
            runner_factory(kinds or core_registry(), self.services),
            resources.settings.engine,
            error_handler_factory=error_handler,
            saver=self.saver,
        )
        self.graphs = GraphCache(
            lambda b, role: compiler.compile(b, role=role), resources.manifest, caches.graphs
        )
        self.subflows.bind(self)

    def tool_catalog_version(self) -> str:
        """Part of the compiled graphs' cache key: a changed MCP tool catalog recompiles (G-08)."""
        mcp = self.services.ports.mcp
        return str(mcp.version) if mcp is not None else "0"

    async def prepare(self, agentic_workflow_id: str, caller: Caller) -> Prepared:
        loaded = await self.loader.load_cached(agentic_workflow_id, caller.workflows())
        blueprint = self.blueprints.get(loaded)
        self.hooks.runner.resolver.of(blueprint)  # a document binding that is not valid: DOCUMENT_INVALID
        graph = self.graphs.get(blueprint, tool_catalog_version=self.tool_catalog_version())
        return Prepared(loaded=loaded, blueprint=blueprint, graph=graph)

    def prepare_stored(self, stored: Any, agentic_workflow_id: str) -> Prepared:
        """A stored document's graph: a paused run's revision, whatever the builder saved since (H-09)."""
        loaded = self.loader.from_stored(stored, agentic_workflow_id)
        blueprint = self.blueprints.get(loaded)
        graph = self.graphs.get(blueprint, tool_catalog_version=self.tool_catalog_version())
        return Prepared(loaded=loaded, blueprint=blueprint, graph=graph)
