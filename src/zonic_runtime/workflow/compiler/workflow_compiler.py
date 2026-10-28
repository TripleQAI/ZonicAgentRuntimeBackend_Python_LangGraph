"""``WorkflowCompiler``: a blueprint to a compiled ``StateGraph`` (design 6.1, 6.3; plan P1-09).

One LangGraph node per compiled node spec (reachable, outside containers, not ignored),
with the manifest's policies and the blueprint's join decision, and one static edge:
``START`` to the entry. Everything else is ``Command`` routing from the edge index, so
``destinations`` are declared for rendering only (``get_graph()`` shows the document's
topology). Building a graph calls no model and holds no run data (rule 14): node actions
come from ``runner_factory`` and read the run through ``runtime.context``.

The role decides the checkpointer (DR-3): a ``root`` graph is compiled with the turn saver,
a ``child`` graph (sub-flow) with ``checkpointer=None`` so it inherits the parent's per
invocation.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from langgraph.graph import START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import RetryPolicy, TimeoutPolicy

from zonic_runtime.engine.context import RunContext
from zonic_runtime.engine.errors import error_code
from zonic_runtime.engine.state import RunState
from zonic_runtime.models.blueprint import Blueprint, NodeSpec
from zonic_runtime.models.resources.manifest import RetrySpec
from zonic_runtime.models.resources.settings import EngineSettings

Role = Literal["root", "child"]
#: ``(spec, blueprint) -> node action``; P1-10's ``WorkflowNodeRunner``, or a scripted test kind.
RunnerFactory = Callable[[NodeSpec, Blueprint], Any]
#: ``(spec, blueprint, node action) -> error handler`` (P1-10's ``ErrorPolicyHandler``).
ErrorHandlerFactory = Callable[[NodeSpec, Blueprint, Any], Any]


def retry_policy(spec: RetrySpec | None) -> RetryPolicy | None:
    """A ``RetryPolicy`` that retries only the catalog codes the manifest lists (design 8.8), and
    with ``methods`` (the http kind) only errors of those HTTP methods: a POST is never repeated."""
    if spec is None or spec.max_attempts <= 1:
        return None
    codes = frozenset(spec.on)
    methods = frozenset(m.upper() for m in spec.methods)

    def retry_on(error: Exception) -> bool:
        if error_code(error) not in codes:
            return False
        if not methods:
            return True
        method = getattr(error, "details", {}).get("method")
        return isinstance(method, str) and method.upper() in methods

    options: dict[str, Any] = {"max_attempts": spec.max_attempts, "retry_on": retry_on}
    if spec.initial_interval_seconds is not None:
        options["initial_interval"] = spec.initial_interval_seconds
    if spec.backoff_factor is not None:
        options["backoff_factor"] = spec.backoff_factor
    if spec.max_interval_seconds is not None:
        options["max_interval"] = spec.max_interval_seconds
    if spec.jitter is not None:
        options["jitter"] = spec.jitter
    return RetryPolicy(**options)


def timeout_policy(spec: NodeSpec, default_seconds: float, *, owned: bool = False) -> TimeoutPolicy:
    """The node's ``TimeoutPolicy``; a kind that owns its deadline (``owns_deadline``) keeps only
    the idle timeout."""
    run_timeout = None if owned else spec.timeout_seconds or default_seconds
    return TimeoutPolicy(run_timeout=run_timeout, idle_timeout=spec.idle_timeout_seconds)


def destinations(blueprint: Blueprint, spec: NodeSpec) -> dict[str, str]:
    """Target graph name -> the handles leading there, for ``get_graph()``."""
    labels: dict[str, list[str]] = {}
    for handle, node_ids in blueprint.edge_index.edges.get(spec.node_id, {}).items():
        for node_id in node_ids:
            labels.setdefault(blueprint.nodes[node_id].graph_name, []).append(handle)
    return {target: ", ".join(handles) for target, handles in labels.items()}


class WorkflowCompiler:
    def __init__(
        self,
        runner_factory: RunnerFactory,
        engine: EngineSettings,
        *,
        error_handler_factory: ErrorHandlerFactory | None = None,
        saver: Any = None,
    ) -> None:
        self._runner = runner_factory
        self._error_handler = error_handler_factory
        self._engine = engine
        self._saver = saver

    def compile(self, blueprint: Blueprint, *, role: Role = "root") -> CompiledStateGraph[Any, Any, Any, Any]:
        builder: StateGraph[Any, Any, Any, Any] = StateGraph(RunState, context_schema=RunContext)
        builder.set_node_defaults(timeout=TimeoutPolicy(run_timeout=self._engine.node_timeout_seconds))
        for spec in blueprint.compiled_nodes():
            action = self._runner(spec, blueprint)
            builder.add_node(
                spec.graph_name,
                action,
                defer=spec.join.barrier,
                retry_policy=retry_policy(spec.retry),
                timeout=timeout_policy(
                    spec,
                    self._engine.node_timeout_seconds,
                    owned=getattr(getattr(action, "kind", None), "owns_deadline", False),
                ),
                error_handler=self._error_handler(spec, blueprint, action) if self._error_handler else None,
                destinations=destinations(blueprint, spec) or None,
                metadata=spec.trace_metadata(),
            )
        builder.add_edge(START, blueprint.nodes[blueprint.entry].graph_name)
        return builder.compile(checkpointer=self._saver if role == "root" else None, name=blueprint.graph_id)
