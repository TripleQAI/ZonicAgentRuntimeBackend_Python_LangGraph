"""``HookResolver``: the bindings of one blueprint, by hook point and node (design 13; plan P2-06).

Ported from the Node.js runtime's ``hooks/resolver.ts``.

* **Sources:** global bindings from ``resources/hooks.json`` (validated when the resolver is
  built, so a bad file stops start-up with its path, case K-15), workflow bindings from the
  document's ``runtimeHooks``, and node bindings from a node's ``properties.hooks`` (they apply to
  that node only).
* **Document bindings** may use only ``settings.hooks.documentAllowedTypes``; anything wrong in
  one is ``DOCUMENT_INVALID`` with its path (case K-10).
* **Matching** (case K-01): every non-empty ``match`` list must match the node; tags match when
  any one does. Run-level points take bindings that name no nodes.
* **Disabling:** a node's ``properties.disabledHooks`` turns off an overridable binding; a
  non-overridable one still runs, with a warning (case K-09).
* **Order** (case K-03): by ``order``, then source (global, workflow, node).

Resolution is cached per blueprint revision.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, get_args

from pydantic import BaseModel, ValidationError

from zonic_runtime.errors import ZonicError
from zonic_runtime.hooks.registry import HookRegistry
from zonic_runtime.hooks.types import NodeHook
from zonic_runtime.models.blueprint import Blueprint, NodeSpec
from zonic_runtime.models.hooks.binding import HookBinding, HookBindings, HookMatch, HookPoint
from zonic_runtime.models.resources.settings import HookSettings
from zonic_runtime.runtime.lru import LruCache

HookSource = Literal["global", "workflow", "node"]  # principle: allow rule-1-node-type binding-source
_RANK: Mapping[str, int] = MappingProxyType(
    {source: rank for rank, source in enumerate(get_args(HookSource))}
)
RUN_POINTS: frozenset[str] = frozenset({"beforeRun", "afterRun", "onRunError"})
_GLOBAL, _WORKFLOW, _NODE = get_args(HookSource)
_MAX_RESOLVED = 512


@dataclass(frozen=True)
class ResolvedBinding:
    binding: HookBinding
    hook: NodeHook
    config: BaseModel
    source: HookSource


@dataclass(frozen=True)
class ResolvedHooks:
    nodes: dict[str, dict[str, list[ResolvedBinding]]] = field(default_factory=dict)
    runs: dict[str, list[ResolvedBinding]] = field(default_factory=dict)
    #: Node ID -> the non-overridable bindings it tried to turn off.
    not_overridable: dict[str, list[str]] = field(default_factory=dict)

    def node(self, point: HookPoint, node_id: str) -> list[ResolvedBinding]:
        return self.nodes.get(node_id, {}).get(point, [])

    def run(self, point: HookPoint) -> list[ResolvedBinding]:
        return self.runs.get(point, [])


def _issues(error: ValidationError, path: str) -> list[dict[str, str]]:
    return [
        {"path": ".".join([path, *(str(p) for p in e["loc"])]), "message": str(e["msg"])}
        for e in error.errors()
    ]


class HookResolver:
    def __init__(self, registry: HookRegistry, bindings: HookBindings, settings: HookSettings) -> None:
        self._registry = registry
        self._settings = settings
        issues: list[dict[str, str]] = []
        self._global = [
            r
            for i, b in enumerate(bindings.bindings)
            for r in self._prepare(b, _GLOBAL, f"hooks.json.bindings[{i}]", issues)
        ]
        if issues:
            raise ZonicError("RESOURCE_INVALID", file="hooks.json", issues=issues)
        self._cache: LruCache[tuple[str, str], ResolvedHooks] = LruCache(_MAX_RESOLVED)

    def of(self, blueprint: Blueprint) -> ResolvedHooks:
        key = (blueprint.workflow_id, blueprint.revision)
        found = self._cache.get(key)
        if found is None:
            found = self._resolve(blueprint)
            self._cache.put(key, found)
        return found

    def _resolve(self, blueprint: Blueprint) -> ResolvedHooks:
        issues: list[dict[str, str]] = []
        workflow = [
            r
            for i, raw in enumerate(blueprint.runtime_hooks)
            for r in self._parse(raw, _WORKFLOW, f"runtimeHooks[{i}]", issues)
        ]
        shared = [*self._global, *workflow]
        nodes: dict[str, dict[str, list[ResolvedBinding]]] = {}
        not_overridable: dict[str, list[str]] = {}
        for spec in blueprint.nodes.values():
            own = [
                r
                for i, raw in enumerate(spec.hooks)
                for r in self._parse(
                    raw, _NODE, f"nodes[{spec.node_id}].properties.hooks[{i}]", issues, own=True
                )
            ]
            disabled = set(spec.disabled_hooks)
            kept = []
            for item in shared:
                if not self._matches(item.binding.match, spec, blueprint):
                    continue
                if item.binding.id in disabled:
                    if item.binding.overridable:
                        continue
                    not_overridable.setdefault(spec.node_id, []).append(item.binding.id)
                kept.append(item)
            nodes[spec.node_id] = self._index([*kept, *own], lambda p: p not in RUN_POINTS)
        if issues:
            raise ZonicError("DOCUMENT_INVALID", issues=issues)
        run_level = [i for i in shared if self._matches_run(i.binding.match, blueprint)]
        return ResolvedHooks(
            nodes=nodes,
            runs=self._index(run_level, lambda p: p in RUN_POINTS),
            not_overridable=not_overridable,
        )

    @staticmethod
    def _index(bindings: Sequence[ResolvedBinding], include: Any) -> dict[str, list[ResolvedBinding]]:
        by_point: dict[str, list[ResolvedBinding]] = {}
        for item in bindings:
            for point in item.binding.on:
                if include(point):
                    by_point.setdefault(point, []).append(item)
        for found in by_point.values():
            found.sort(key=lambda r: (r.binding.order, _RANK[r.source]))
        return by_point

    @staticmethod
    def _matches(match: HookMatch, spec: NodeSpec, blueprint: Blueprint) -> bool:
        return (
            (not match.node_kinds or spec.kind in match.node_kinds)
            and (not match.node_types or spec.node_type in match.node_types)
            and (not match.node_names or spec.name in match.node_names)
            and (not match.workflow_ids or blueprint.workflow_id in match.workflow_ids)
            and (not match.tags or not set(match.tags).isdisjoint(spec.tags))
        )

    @staticmethod
    def _matches_run(match: HookMatch, blueprint: Blueprint) -> bool:
        names_nodes = match.node_kinds or match.node_types or match.node_names or match.tags
        return not names_nodes and (not match.workflow_ids or blueprint.workflow_id in match.workflow_ids)

    def _parse(
        self, raw: Any, source: HookSource, path: str, issues: list[dict[str, str]], *, own: bool = False
    ) -> list[ResolvedBinding]:
        try:
            binding = HookBinding.model_validate(raw)
        except ValidationError as exc:
            issues += _issues(exc, path)
            return []
        if own:
            binding = binding.model_copy(update={"match": HookMatch()})
        if binding.hook not in self._settings.document_allowed_types:
            issues.append(
                {"path": f"{path}.hook", "message": f"hook type {binding.hook} is not allowed in documents"}
            )
            return []
        return self._prepare(binding, source, path, issues)

    def _prepare(
        self, binding: HookBinding, source: HookSource, path: str, issues: list[dict[str, str]]
    ) -> list[ResolvedBinding]:
        if not binding.enabled:
            return []
        hook = self._registry.get(binding.hook)
        if hook is None:
            issues.append({"path": f"{path}.hook", "message": f"unknown hook type {binding.hook}"})
            return []
        try:
            config = hook.config.model_validate(binding.config)
        except ValidationError as exc:
            issues += _issues(exc, f"{path}.config")
            return []
        problem = hook.check(config)
        if problem:
            issues.append({"path": f"{path}.config", "message": problem})
            return []
        return [ResolvedBinding(binding=binding, hook=hook, config=config, source=source)]
