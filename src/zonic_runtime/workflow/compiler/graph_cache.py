"""Compiled graphs by workflow, revision, role, manifest version and tool catalog version
(design 6.8; plan P1-09, DR-3, cases D-09, G-07, G-08, G-13).

Concurrent runs of one revision share one compiled graph; nothing in it belongs to a run
(rule 14), so sharing is safe (case G-07). The role is part of the key because a workflow
can be both a catalog root (compiled with the turn saver) and another workflow's sub-flow
(compiled without one).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from zonic_runtime.models.blueprint import Blueprint
from zonic_runtime.models.resources.manifest import NodeRuntimeManifest
from zonic_runtime.runtime.lru import LruCache
from zonic_runtime.workflow.compiler.workflow_compiler import Role
from zonic_runtime.workflow.loader.revision import revision_hash


@dataclass(frozen=True)
class GraphKey:
    workflow_id: str
    revision: str
    role: Role
    manifest_version: str
    tool_catalog_version: str


def manifest_version(manifest: NodeRuntimeManifest) -> str:
    """A content hash of the manifest: any change to node types, kinds or policies recompiles."""
    return revision_hash(manifest.model_dump(mode="json", by_alias=True))[:16]


class GraphCache:
    def __init__(
        self, compile_graph: Callable[[Blueprint, Role], Any], manifest: NodeRuntimeManifest, max_entries: int
    ) -> None:
        self._compile = compile_graph
        self._manifest_version = manifest_version(manifest)
        self._entries: LruCache[GraphKey, Any] = LruCache(max_entries)
        #: Compilations performed (cache misses); tests read it.
        self.compilations = 0

    def key(self, blueprint: Blueprint, role: Role, tool_catalog_version: str = "0") -> GraphKey:
        return GraphKey(
            blueprint.workflow_id, blueprint.revision, role, self._manifest_version, tool_catalog_version
        )

    def get(self, blueprint: Blueprint, role: Role = "root", tool_catalog_version: str = "0") -> Any:
        key = self.key(blueprint, role, tool_catalog_version)
        graph = self._entries.get(key)
        if graph is None:
            graph = self._compile(blueprint, role)
            self.compilations += 1
            self._entries.put(key, graph)
        return graph

    def __len__(self) -> int:
        return len(self._entries)
