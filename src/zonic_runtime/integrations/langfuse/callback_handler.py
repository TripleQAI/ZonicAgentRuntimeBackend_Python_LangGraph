"""``ZonicLangfuseCallbackHandler`` and ``ObservationBridge`` (design 12; plan P2-07, DR-5, SP-4).

The handler is Langfuse's LangChain ``CallbackHandler``, one per run, with three changes:

* **Node spans are marked.** A chain run whose parent is a graph run is a builder node visit (or
  a node's error handler, ``__error_handler__<node>``). The graph runs are the root run and, for a
  sub-flow node's visit, the child graph run inside it (its blueprint from ``children``). It gets
  ``zonic_node_span`` and
  ``zonic_visit`` (from the state's visit counts) in its metadata, so the export can type it from
  the manifest and number visits (cases L-02, L-04). Spans inside the node (the agent graph, model
  and tool calls) carry the node's metadata too (SP-4) but are not marked.
* **A handled failure is a warning.** A node whose error policy is ``continue`` or ``route`` ends
  its observation with level ``WARNING`` and the code; ``fail_run`` keeps ``ERROR`` (case L-07).
* **Error handlers belong to their node** (DR-5): the error handler's run is marked as the failed
  node's, so its observation (the error policy's decision) is attributed to that node.

The handler's own observation types are not used (SP-4): the export retypes node spans.

``ObservationBridge`` adds ``event`` observations under a node's observation from inside the
node: the node's config carries its run ID as the callback manager's parent run.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from uuid import UUID

from langfuse.langchain import CallbackHandler
from opentelemetry import trace

from zonic_runtime.models.blueprint import Blueprint, NodeSpec

#: ``(parent blueprint, sub-flow node ID) -> child blueprint``.
ChildLookup = Callable[[Blueprint, str], Blueprint | None]

NODE_SPAN, VISIT, ERROR_HANDLER = "zonic_node_span", "zonic_visit", "zonic_error_handler"
ERROR_HANDLER_PREFIX = "__error_handler__"
#: Error policies that handle a failure: the node's observation is a warning.
_HANDLED = frozenset({"continue", "route"})
#: Codes that end the run whatever the policy (``engine/errors.RUN_FATAL``).
_FATAL = frozenset({"LOOP_LIMIT_EXCEEDED", "RUN_TIMEOUT", "CANCELLED"})
_EVENT = "event"


def _code(error: BaseException) -> str:
    code = getattr(error, "code", None)
    return code if isinstance(code, str) else type(error).__name__


class ZonicLangfuseCallbackHandler(CallbackHandler):
    def __init__(self, *, public_key: str, blueprint: Blueprint, children: ChildLookup | None = None) -> None:
        super().__init__(public_key=public_key)
        self._blueprint = blueprint
        self._children: ChildLookup = children or (lambda _parent, _node: None)
        self._root: UUID | None = None
        self._policies: dict[UUID, str] = {}
        #: Graph runs (the root, child graphs of sub-flows) and their blueprints.
        self._graphs: dict[UUID, Blueprint] = {}
        #: Node visit runs: their graph's blueprint and spec, so a child graph run is recognised.
        self._nodes: dict[UUID, tuple[Blueprint, NodeSpec]] = {}

    def node_spec(self, name: str, blueprint: Blueprint | None = None) -> tuple[NodeSpec | None, bool]:
        handler = name.startswith(ERROR_HANDLER_PREFIX)
        graph = blueprint or self._blueprint
        return graph.by_graph_name(name.removeprefix(ERROR_HANDLER_PREFIX)), handler

    def on_chain_start(
        self,
        serialized: dict[str, Any] | None,
        inputs: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        name = self.get_langchain_run_name(serialized, **kwargs)
        if self._root is None and parent_run_id is None:
            self._root = run_id
            self._graphs[run_id] = self._blueprint
        elif parent_run_id is not None and parent_run_id in self._graphs:
            graph = self._graphs[parent_run_id]
            spec, handler = self.node_spec(name, graph)
            if spec is not None:
                visits = inputs.get("visits") if isinstance(inputs, Mapping) else None
                visit = int((visits or {}).get(spec.node_id, 0)) + 1
                metadata = {**(metadata or {}), NODE_SPAN: True, VISIT: visit, ERROR_HANDLER: handler}
                if not handler:
                    self._policies[run_id] = spec.error_policy
                    self._nodes[run_id] = (graph, spec)
        elif parent_run_id is not None and parent_run_id in self._nodes:
            graph, spec = self._nodes[parent_run_id]
            child = self._children(graph, spec.node_id)
            if child is not None and name == child.graph_id:  # the sub-flow's child graph
                self._graphs[run_id] = child
        return super().on_chain_start(
            serialized,
            inputs,
            run_id=run_id,
            parent_run_id=parent_run_id,
            tags=tags,
            metadata=metadata,
            **kwargs,
        )

    def on_chain_end(
        self, outputs: Any, *, run_id: UUID, parent_run_id: UUID | None = None, **kwargs: Any
    ) -> Any:
        self._policies.pop(run_id, None)
        self._nodes.pop(run_id, None)
        self._graphs.pop(run_id, None)
        return super().on_chain_end(outputs, run_id=run_id, parent_run_id=parent_run_id, **kwargs)

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        policy = self._policies.pop(run_id, None)
        self._nodes.pop(run_id, None)
        self._graphs.pop(run_id, None)
        code = _code(error)
        if policy not in _HANDLED or code in _FATAL or parent_run_id is None:
            return super().on_chain_error(
                error, run_id=run_id, parent_run_id=parent_run_id, tags=tags, **kwargs
            )
        observation = self._detach_observation(run_id)
        if observation is not None:
            message = f"{code}: handled by the {policy} error policy"
            observation.update(level="WARNING", status_message=message, input=kwargs.get("inputs")).end()
        return None

    def observation(self, run_id: UUID | None) -> Any:
        return self._runs.get(run_id) if run_id is not None else None


class ObservationBridge:
    def __init__(self, handler: ZonicLangfuseCallbackHandler) -> None:
        self._handler = handler

    def parent(self, config: Any, fallback: Any) -> Any:
        """The node observation of ``config``; ``fallback`` (the run's root) when it is not known."""
        callbacks = config.get("callbacks") if isinstance(config, Mapping) else None
        return self._handler.observation(getattr(callbacks, "parent_run_id", None)) or fallback

    def event(
        self,
        config: Any,
        name: str,
        level: str,
        metadata: Mapping[str, Any] | None,
        fallback: Any,
        *,
        as_type: str = _EVENT,
    ) -> None:
        """An event (or a finished ``span``) under the node observation of ``config``; under
        ``fallback`` (the run's root observation) when the node's observation is not known."""
        parent = self.parent(config, fallback)
        if parent is None:
            return
        with trace.use_span(parent._otel_span, end_on_exit=False):
            child = parent.start_observation(
                name=name, as_type=as_type, level=level, metadata=dict(metadata or {})
            )
            if as_type != _EVENT:
                child.end()
