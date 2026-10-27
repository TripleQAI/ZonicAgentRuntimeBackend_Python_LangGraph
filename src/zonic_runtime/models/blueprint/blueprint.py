"""The blueprint: the immutable analysis of one document revision (design 6.2; plan P1-02).

P1-05 builds it from a normalized document and the manifest; the compiler (P1-09) reads
it. It holds no LangGraph objects, so it can be cached by revision hash, compared in
tests and shown in diagnostics.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict, Field, SerializeAsAny

from zonic_runtime.models.base import ZonicModel
from zonic_runtime.models.io.ports import NodeIoSpec
from zonic_runtime.models.nodes.base import NodeConfigBase
from zonic_runtime.models.resources.manifest import ErrorPolicy, JoinPolicy, ObservationSpec, RetrySpec

#: Kind of the nodes that are never compiled (notes).
IGNORED_KIND = "ignore"


class _Frozen(ZonicModel):
    model_config = ConfigDict(frozen=True)


class BlueprintWarning(_Frozen):
    """A finding about the document. ``error`` findings fail the run with ``PLAN_INVALID``."""

    code: str
    severity: Literal["error", "warning", "info"] = "warning"
    message: str
    node_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class JoinSpec(_Frozen):
    """Fan-in decision for one node (design 6.5): ``barrier`` becomes ``defer=True``."""

    policy: JoinPolicy = "auto"
    barrier: bool = False
    sources: list[str] = Field(default_factory=list)


class RunLimits(_Frozen):
    """Visit and step limits of a run of this document: the document's own, capped (design 6.4)."""

    max_visits_per_node: int = Field(gt=0)
    max_steps: int = Field(gt=0)


class Component(_Frozen):
    """A strongly connected component with a cycle: one loop (design 6.6)."""

    loop_id: str
    nodes: list[str]
    entries: list[str] = Field(default_factory=list)
    has_exit: bool = True


class EdgeIndex(_Frozen):
    """Edges by ``(source, handle)`` (design 6.4): ``edges[node_id][handle] -> [target IDs]``."""

    edges: dict[str, dict[str, list[str]]] = Field(default_factory=dict)

    def targets(self, node_id: str, handle: str) -> list[str]:
        return list(self.edges.get(node_id, {}).get(handle, []))

    def handles(self, node_id: str) -> list[str]:
        return list(self.edges.get(node_id, {}))

    def all_targets(self, node_id: str) -> list[str]:
        seen: list[str] = []
        for targets in self.edges.get(node_id, {}).values():
            seen += [t for t in targets if t not in seen]
        return seen


class NodeSpec(_Frozen):
    """One node as the compiler builds it."""

    node_id: str
    node_type: str
    kind: str
    name: str
    graph_name: str
    config: SerializeAsAny[NodeConfigBase]
    handles: list[str] = Field(default_factory=list)
    optional_handles: list[str] = Field(default_factory=list)
    destinations: list[str] = Field(default_factory=list)
    join: JoinSpec = Field(default_factory=JoinSpec)
    retry: RetrySpec | None = None
    timeout_seconds: float | None = Field(default=None, gt=0)
    idle_timeout_seconds: float | None = Field(default=None, gt=0)
    error_policy: ErrorPolicy = "fail_run"
    reachable: bool = True
    loop_id: str | None = None
    container: str | None = None
    terminal: bool = False
    #: The type's ports and output (manifest ``io``).
    io: NodeIoSpec | None = None
    #: The effective output schema: ``allOf(payload, extensions, node outputSchema)`` (design 8.2).
    output_schema: dict[str, Any] | None = None
    #: Langfuse observation type and capture (manifest defaults, then the type's entry).
    observation: ObservationSpec | None = None

    @property
    def compiled(self) -> bool:
        """Compiled into the workflow graph: reachable, not a swarm member, not ignored."""
        return self.reachable and self.container is None and self.kind != IGNORED_KIND

    def trace_metadata(self) -> dict[str, Any]:
        """``add_node(metadata=...)``; the exporter types spans from it (decision 0001, SP-4)."""
        meta: dict[str, Any] = {
            "zonic_node_id": self.node_id,
            "zonic_node_type": self.node_type,
            "zonic_kind": self.kind,
        }
        if self.loop_id:
            meta["zonic_loop_id"] = self.loop_id
        if self.observation is not None:
            meta["zonic_observation_type"] = self.observation.type
        return meta


class Blueprint(_Frozen):
    workflow_id: str
    revision: str
    document_version: str
    graph_id: str
    entry: str
    nodes: dict[str, NodeSpec]
    edge_index: EdgeIndex
    limits: RunLimits
    components: list[Component] = Field(default_factory=list)
    name_index: dict[str, str] = Field(default_factory=dict)
    children: list[str] = Field(default_factory=list)
    warnings: list[BlueprintWarning] = Field(default_factory=list)

    @property
    def errors(self) -> list[BlueprintWarning]:
        return [w for w in self.warnings if w.severity == "error"]

    def compiled_nodes(self) -> list[NodeSpec]:
        return [spec for spec in self.nodes.values() if spec.compiled]

    def resolve(self, name_or_id: str) -> NodeSpec | None:
        """``{{nodes.<name or id>}}`` -> the node, through the name index (design 5.3)."""
        node_id = self.name_index.get(name_or_id, name_or_id)
        return self.nodes.get(node_id)

    def loop_entry(self, node_id: str) -> Component | None:
        """The loop this node enters, when it is one of the loop's entry nodes."""
        return next((c for c in self.components if node_id in c.entries), None)

    def by_graph_name(self, graph_name: str) -> NodeSpec | None:
        return next((s for s in self.nodes.values() if s.graph_name == graph_name), None)
