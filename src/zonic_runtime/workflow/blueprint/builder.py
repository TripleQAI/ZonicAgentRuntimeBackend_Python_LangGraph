"""``BlueprintBuilder``: the analysis of one loaded document revision (design 6.2; plan P1-05).

Ported in structure from the Node.js runtime's ``blueprint-builder.ts`` and adapted to
LangGraph: cycles stay as they are (components only scope visits and tracing), and joins
after a parallel fan-out become barriers (``defer=True``). Node roles come from the
builder registry's node semantics and handles from the registry and the manifest, so no
node type or handle is named here (design rule 1).

Every error is collected before failing. The run fails with ``PLAN_INVALID`` listing each
problem, or with the problems' own catalog code when they all share one (for example
``NODE_TYPE_UNSUPPORTED`` or ``RULE_EXPRESSION_UNSUPPORTED``).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, get_args

from pydantic import ValidationError

from zonic_runtime.config.settings import RuntimeResources
from zonic_runtime.errors import ZonicError
from zonic_runtime.io.output_schemas import effective_output_schema, known_output_schema
from zonic_runtime.io.schema_registry import SchemaRegistry
from zonic_runtime.models.blueprint import (
    IGNORED_KIND,
    Blueprint,
    BlueprintWarning,
    Component,
    EdgeIndex,
    JoinSpec,
    NodeSpec,
    RunLimits,
)
from zonic_runtime.models.document import WorkflowNode
from zonic_runtime.models.engine import StateUpdate
from zonic_runtime.models.engine.records import ALL_HANDLES
from zonic_runtime.models.nodes import NodeConfigBase, config_model_for
from zonic_runtime.models.resources.manifest import ErrorPolicy, JoinPolicy, NodeTypeEntry
from zonic_runtime.runtime.lru import LruCache
from zonic_runtime.runtime.paths import JsonObject, get_path
from zonic_runtime.workflow.blueprint.graph import cyclic, fan_in_joins, graph_name, strongly_connected
from zonic_runtime.workflow.blueprint.rules import BuilderRuleEvaluator, RuleEdge, RuleNode
from zonic_runtime.workflow.blueprint.template_checker import TemplateChecker
from zonic_runtime.workflow.loader.loader import LoadedDocument
from zonic_runtime.workflow.templates.resolver import TemplateResolver

_JOIN_POLICIES: tuple[str, ...] = get_args(JoinPolicy)
_ERROR_POLICIES: tuple[str, ...] = get_args(ErrorPolicy)
_GENERIC = "PLAN_INVALID"
#: Configuration lists holding canonical ``StateUpdate`` entries after normalization.
_UPDATE_LISTS = ("variableUpdates", "stateUpdates")


def _strings(value: Any) -> list[str]:
    return [v for v in value if isinstance(v, str) and v] if isinstance(value, list) else []


@dataclass
class _Draft:
    """A node while the blueprint is built; frozen into a ``NodeSpec`` at the end."""

    node: WorkflowNode
    raw: JsonObject
    entry: NodeTypeEntry
    config: NodeConfigBase
    members: list[str]
    container: str | None = None
    reachable: bool = False

    @property
    def id(self) -> str:
        return self.node.id


@dataclass(frozen=True)
class _Edge:
    id: str
    source: str
    target: str
    handle: str


class BlueprintBuilder:
    def __init__(self, resources: RuntimeResources, schemas: SchemaRegistry | None = None) -> None:
        self._r = resources
        self._rules = BuilderRuleEvaluator(resources.builder)
        self._catalog_codes = set(resources.errors.errors)
        self._schemas = schemas or SchemaRegistry(
            resources.manifest, resources.builder, resources.settings.caches.validators
        )
        self._checker = TemplateChecker(
            self._schemas,
            TemplateResolver(resources.templates),
            resources.templates,
            "error" if resources.settings.io.template_path_unknown == "error" else "warning",
        )

    def build(self, loaded: LoadedDocument) -> Blueprint:
        findings: list[BlueprintWarning] = list(loaded.findings)
        add = findings.append
        manifest, semantics = self._r.manifest, self._r.builder.semantics
        defaults = manifest.defaults

        def problem(code: str, message: str, path: str, node_id: str | None = None, **details: Any) -> None:
            add(
                BlueprintWarning(
                    code=code,
                    severity="error",
                    message=message,
                    node_id=node_id,
                    details={"path": path, **details},
                )
            )

        def warn(
            code: str, message: str, path: str, node_id: str | None = None, severity: Any = "warning"
        ) -> None:
            add(
                BlueprintWarning(
                    code=code, severity=severity, message=message, node_id=node_id, details={"path": path}
                )
            )

        # Node types and configurations.
        drafts: dict[str, _Draft] = {}
        skipped: set[str] = set()
        features = self._r.settings.features.model_dump(by_alias=True)
        for node in loaded.document.nodes:
            path = f"nodes[{node.id}]"
            if node.id in drafts or node.id in skipped:
                problem("NODE_DUPLICATE", f"node ID {node.id} is used twice", path, node.id)
                continue
            entry = manifest.node_types.get(node.type)
            if entry is None:
                if manifest.on_unknown_type == "fail_plan":
                    problem(
                        "NODE_TYPE_UNSUPPORTED",
                        f"node type {node.type} is not supported",
                        path,
                        node.id,
                        type=node.type,
                    )
                else:
                    warn(
                        "NODE_TYPE_SKIPPED",
                        f"node type {node.type} is not supported and is skipped",
                        path,
                        node.id,
                    )
                skipped.add(node.id)
                continue
            if entry.enabled_by and not features.get(entry.enabled_by):
                problem(
                    "FEATURE_DISABLED",
                    f"node type {node.type} is turned off ({entry.enabled_by})",
                    path,
                    node.id,
                    type=node.type,
                    feature=entry.enabled_by,
                )
                skipped.add(node.id)
                continue
            if entry.kind == IGNORED_KIND:
                skipped.add(node.id)
                continue
            raw = node.model_dump(by_alias=True, exclude_none=True)
            swarm = semantics.swarms.get(node.type)
            drafts[node.id] = _Draft(
                node=node,
                raw=raw,
                entry=entry,
                config=config_model_for(entry.kind).model_validate(node.config),
                members=_strings(get_path(raw, swarm.members_path)) if swarm else [],
            )
            for key in _UPDATE_LISTS:
                for i, update in enumerate(node.config.get(key) or []):
                    try:
                        StateUpdate.model_validate(update)
                    except ValidationError as exc:
                        problem(
                            "UPDATE_INVALID",
                            f"{key}[{i}] of {node.label}: {exc.errors()[0]['msg']}",
                            f"nodes[{node.id}].config.{key}[{i}]",
                            node.id,
                        )

        # Edges; a missing handle is the manifest's default handle.
        edges: list[_Edge] = []
        for i, stored in enumerate(loaded.document.edges):
            edge_id = stored.id or f"edge-{i}"
            if stored.source in skipped or stored.target in skipped:
                warn(
                    "EDGE_IGNORED_NODE",
                    f"edge {edge_id} connects a node that is not compiled",
                    f"edges[{edge_id}]",
                )
                continue
            missing = [end for end in (stored.source, stored.target) if end not in drafts]
            for end in missing:
                problem(
                    "EDGE_UNKNOWN_NODE", f"edge {edge_id} refers to missing node {end}", f"edges[{edge_id}]"
                )
            if not missing:
                edges.append(
                    _Edge(
                        edge_id, stored.source, stored.target, stored.source_handle or defaults.default_handle
                    )
                )

        # Entry node.
        entries = [d.id for d in drafts.values() if semantics.entry.get(d.node.type)]
        if len(entries) != 1:
            problem(
                "ENTRY_INVALID",
                f"a workflow needs exactly one entry node; found {len(entries)}",
                "nodes",
                found=entries,
            )

        # Containers, then the builder's rules (SW-1 to SW-12).
        for draft in drafts.values():
            for member in draft.members:
                if member in drafts and drafts[member].container is None:
                    drafts[member].container = draft.id
        rule_nodes = [RuleNode(d.id, d.node.type, d.node.name or "", d.raw) for d in drafts.values()]
        findings += self._rules.evaluate(rule_nodes, [RuleEdge(e.id, e.source, e.target) for e in edges])
        self._fail_on_errors(findings)
        entry_id = entries[0]

        # Reachability from the entry; container members follow their container.
        queue = [entry_id]
        drafts[entry_id].reachable = True
        while queue:
            current = queue.pop(0)
            for target in [e.target for e in edges if e.source == current] + drafts[current].members:
                if target in drafts and not drafts[target].reachable:
                    drafts[target].reachable = True
                    queue.append(target)
        for draft in drafts.values():
            if not draft.reachable:
                warn(
                    "NODE_UNREACHABLE",
                    f"node {draft.node.label} is unreachable from the entry node and is not compiled",
                    f"nodes[{draft.id}]",
                    draft.id,
                )
        live = [e for e in edges if drafts[e.source].reachable and drafts[e.target].reachable]

        # Handles: what each node can activate, and edges or branches that do not match.
        handles = {d.id: self._handles(d) for d in drafts.values() if d.reachable}
        for edge in live:
            if edge.handle not in handles[edge.source] and edge.handle != ALL_HANDLES:
                warn(
                    "EDGE_UNKNOWN_HANDLE",
                    f"edge {edge.id} leaves {drafts[edge.source].node.label} by handle"
                    f" {edge.handle}, which the node never activates",
                    f"edges[{edge.id}]",
                    edge.source,
                )
        for draft in drafts.values():
            if not draft.reachable:
                continue
            with_edges = {e.handle for e in live if e.source == draft.id}
            branch = semantics.branches.get(draft.node.type)
            if branch:
                for rule_id in _strings(
                    [
                        b.get(branch.id_field)
                        for b in get_path(draft.raw, branch.path) or []
                        if isinstance(b, dict)
                    ]
                ):
                    if rule_id not in with_edges:
                        warn(
                            "HANDLE_NO_EDGE",
                            f"branch {rule_id} of {draft.node.label} has no edge; the run fails"
                            " with ROUTE_UNRESOLVED if it is taken",
                            f"nodes[{draft.id}].{branch.path}",
                            draft.id,
                        )
            for handle in draft.entry.handles.values():
                if handle not in with_edges and handle not in draft.entry.optional_handles:
                    warn(
                        "HANDLE_NO_EDGE",
                        f"handle {handle} of {draft.node.label} has no edge; the run fails"
                        " with ROUTE_UNRESOLVED if it is taken",
                        f"nodes[{draft.id}]",
                        draft.id,
                    )

        # The workflow graph: reachable nodes outside containers.
        graph = [d.id for d in drafts.values() if d.reachable and d.container is None]
        in_graph = set(graph)
        index: dict[str, dict[str, list[str]]] = {}
        for edge in live:
            if edge.source in in_graph and edge.target in in_graph:
                targets = index.setdefault(edge.source, {}).setdefault(edge.handle, [])
                if edge.target not in targets:
                    targets.append(edge.target)

        # Loops (design 6.6) and joins (design 6.5).
        components: list[Component] = []
        loop_of: dict[str, str] = {}
        for component in strongly_connected(graph, index):
            if not cyclic(component, index):
                continue
            members = set(component)
            outside = [
                s
                for s, hs in index.items()
                if s not in members
                for ts in hs.values()
                for t in ts
                if t in members
            ]
            entries_in = [
                n for n in component if any(t == n for s in outside for ts in index[s].values() for t in ts)
            ]
            has_exit = any(
                t not in members for s in component for ts in index.get(s, {}).values() for t in ts
            )
            loop_id = f"loop:{(entries_in or component)[0]}"
            components.append(
                Component(loop_id=loop_id, nodes=component, entries=entries_in, has_exit=has_exit)
            )
            loop_of.update(dict.fromkeys(component, loop_id))
            if not has_exit:
                warn(
                    "LOOP_NO_EXIT",
                    f"loop {loop_id} has no edge leaving it; it ends only at the visit limit",
                    f"nodes[{component[0]}]",
                    component[0],
                )
        joins = fan_in_joins(graph, index)

        # Node specs.
        names = {node_id: graph_name(node_id) for node_id in drafts}
        taken: dict[str, str] = {}
        for node_id, name in names.items():
            if name != node_id:
                warn(
                    "NODE_NAME_ESCAPED",
                    f"node ID {node_id} runs as {name}",
                    f"nodes[{node_id}]",
                    node_id,
                    "info",
                )
            if name in taken:
                problem(
                    "NODE_NAME_COLLISION",
                    f"nodes {taken[name]} and {node_id} share the name {name}",
                    f"nodes[{node_id}]",
                    node_id,
                )
            taken[name] = node_id
        incoming: dict[str, list[str]] = {}
        for source, by_handle in index.items():
            for targets in by_handle.values():
                for target in targets:
                    incoming.setdefault(target, [])
                    if source not in incoming[target]:
                        incoming[target].append(source)
        specs: dict[str, NodeSpec] = {}
        for draft in drafts.values():
            policy = self._join_policy(draft)
            barrier = policy == "barrier" or (policy == "auto" and draft.id in joins)
            if barrier and draft.id in loop_of:
                warn(
                    "JOIN_BARRIER_IN_LOOP",
                    f"join {draft.node.label} waits for every branch, including branches"
                    " outside its loop; joinPolicy any turns this off",
                    f"nodes[{draft.id}]",
                    draft.id,
                    "info",
                )
            specs[draft.id] = NodeSpec(
                node_id=draft.id,
                node_type=draft.node.type,
                kind=draft.entry.kind,
                name=draft.node.label,
                graph_name=names[draft.id],
                config=draft.config,
                handles=handles.get(draft.id, []),
                optional_handles=list(draft.entry.optional_handles),
                destinations=[
                    names[t] for t in dict.fromkeys(t for ts in index.get(draft.id, {}).values() for t in ts)
                ],
                join=JoinSpec(policy=policy, barrier=barrier, sources=incoming.get(draft.id, [])),
                retry=draft.entry.retry or defaults.retry,
                timeout_seconds=self._timeout(draft),
                idle_timeout_seconds=draft.entry.idle_timeout_seconds or defaults.idle_timeout_seconds,
                error_policy=self._error_policy(draft),
                reachable=draft.reachable,
                loop_id=loop_of.get(draft.id),
                container=draft.container,
                terminal=draft.entry.terminal,
                io=draft.entry.io,
                observation=draft.entry.observation or defaults.observation,
                output_schema=effective_output_schema(draft.entry.io, draft.node.config, self._schemas),
            )

        name_index: dict[str, str] = {}
        ambiguous: set[str] = set()
        for spec in specs.values():
            if spec.name in name_index and name_index[spec.name] != spec.node_id:
                ambiguous.add(spec.name)
            name_index.setdefault(spec.name, spec.node_id)
        for name in sorted(ambiguous):
            del name_index[name]
            warn("NODE_NAME_AMBIGUOUS", f"two nodes are named {name}; refer to them by ID", "nodes")

        children: list[str] = []
        for draft in drafts.values():
            child_path = semantics.child_reference.get(draft.node.type)
            child = get_path(draft.raw, child_path) if child_path and draft.reachable else None
            if isinstance(child, str) and child and "{{" not in child and child not in children:
                children.append(child)

        # The static template check (design 8.2): node output paths against known schemas.
        known = {d.id: known_output_schema(d.entry.io, d.node.config, self._schemas) for d in drafts.values()}

        def resolve_node(name_or_id: str) -> NodeSpec | None:
            return specs.get(name_index.get(name_or_id, name_or_id))

        configs = {d.id: d.node.config for d in drafts.values() if d.reachable}
        findings += self._checker.check(
            {k: v for k, v in specs.items() if k in configs}, configs, known, resolve_node
        )
        self._fail_on_errors(findings)

        workflow_id = loaded.agentic_workflow_id
        return Blueprint(
            workflow_id=workflow_id,
            revision=loaded.revision,
            document_version=loaded.version,
            graph_id=workflow_id or "root",
            entry=entry_id,
            nodes=specs,
            edge_index=EdgeIndex(edges=index),
            limits=self._limits(loaded),
            components=components,
            name_index=name_index,
            children=children,
            warnings=findings,
        )

    # -- per node ---------------------------------------------------------------

    def _handles(self, draft: _Draft) -> list[str]:
        """Registry and manifest handles, branch IDs, handoffs and routing tools, the error handle."""
        defaults = self._r.manifest.defaults
        semantics = self._r.builder.semantics
        registered = self._r.builder.node_types.get(draft.node.type)
        found = list(registered.source_handles if registered else [defaults.default_handle])
        found += draft.entry.handles.values()
        branch = semantics.branches.get(draft.node.type)
        if branch:
            found += [
                b[branch.id_field]
                for b in get_path(draft.raw, branch.path) or []
                if isinstance(b, dict) and isinstance(b.get(branch.id_field), str)
            ]
            found += self._r.dialects.default_rule_ids
        handoff_path = semantics.handoff_targets.get(draft.node.type)
        targets = _strings(get_path(draft.raw, handoff_path)) if handoff_path else []
        targets += [
            t["target"]
            for t in draft.node.config.get("routingTools") or []
            if isinstance(t, dict) and isinstance(t.get("target"), str)
        ]
        found += [f"{defaults.handoff_handle_prefix}{t}" for t in targets]
        if self._error_policy(draft) == "route":
            found.append(defaults.error_handle)
        return list(dict.fromkeys(found))

    def _join_policy(self, draft: _Draft) -> JoinPolicy:
        own = (draft.node.properties or {}).get("joinPolicy")
        if own in _JOIN_POLICIES:
            return own  # type: ignore[no-any-return]
        return draft.entry.join_policy or self._r.manifest.defaults.join_policy

    def _error_policy(self, draft: _Draft) -> ErrorPolicy:
        handling = draft.node.config.get("errorHandling")
        if isinstance(handling, dict) and handling.get("policy") in _ERROR_POLICIES:
            return handling["policy"]  # type: ignore[no-any-return]
        return draft.entry.error_policy or self._r.manifest.defaults.error_policy

    def _timeout(self, draft: _Draft) -> float:
        own = draft.node.config.get("timeoutSeconds")
        if isinstance(own, int | float) and not isinstance(own, bool) and own > 0:
            return float(own)
        if draft.entry.timeout_seconds_from:
            found = get_path(draft.raw, draft.entry.timeout_seconds_from)
            if isinstance(found, int | float) and not isinstance(found, bool) and found > 0:
                return float(found)
        return draft.entry.timeout_seconds or self._r.manifest.defaults.timeout_seconds

    def _limits(self, loaded: LoadedDocument) -> RunLimits:
        engine = self._r.settings.engine
        own = loaded.document.limits
        visits = own.max_visits_per_node if own and own.max_visits_per_node else engine.max_visits_per_node
        steps = own.max_steps if own and own.max_steps else engine.max_steps
        return RunLimits(
            max_visits_per_node=min(visits, engine.max_limits.max_visits_per_node),
            max_steps=min(steps, engine.max_limits.max_steps),
        )

    def _fail_on_errors(self, findings: Sequence[BlueprintWarning]) -> None:
        errors = [f for f in findings if f.severity == "error"]
        if not errors:
            return
        codes = {f.code for f in errors}
        code = codes.pop() if len(codes) == 1 and codes <= self._catalog_codes else _GENERIC
        raise ZonicError(code, problems=[f.wire() for f in errors])


class BlueprintCache:
    """Blueprints by ``(workflow ID, revision)``, LRU-bounded (cases D-09, D-10)."""

    def __init__(self, build: Callable[[LoadedDocument], Blueprint], max_entries: int) -> None:
        self._build = build
        self._entries: LruCache[tuple[str, str], Blueprint] = LruCache(max_entries)
        #: Analyses performed (cache misses); tests read it.
        self.analyses = 0

    def get(self, loaded: LoadedDocument) -> Blueprint:
        key = (loaded.agentic_workflow_id, loaded.revision)
        cached = self._entries.get(key)
        if cached is not None:
            return cached
        blueprint = self._build(loaded)
        self.analyses += 1
        self._entries.put(key, blueprint)
        return blueprint

    def __len__(self) -> int:
        return len(self._entries)
