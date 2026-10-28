"""``SubflowOpener``: the sub-flow kind's port onto the one build path (design 6.6; plan P3-09, F-10).

A resumed run opens the children it paused with, from its pinned documents (P4-02, case H-09).

A child comes from the loader (``v_agent_export``: children are not catalog entries), the
blueprint cache, and the graph cache in the ``child`` role with the current tool catalog
version. The first child a parent revision opens loads all of that parent's static children with
one ``in`` read, kept ``catalog.cacheSeconds`` (F-10); a child named by a template, or missing
from that read, is read on its own, and one that does not exist is ``WORKFLOW_NOT_FOUND``. A
caller with a valid token may run any stored workflow (decision 0004), so existing is visible.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from zonic_runtime.engine.context import RunDocuments
from zonic_runtime.engine.node_template import ChildWorkflow
from zonic_runtime.errors import ZonicError
from zonic_runtime.models.blueprint import Blueprint
from zonic_runtime.runtime.lru import LruCache
from zonic_runtime.workflow.loader.loader import LoadedDocument

NOT_FOUND = "WORKFLOW_NOT_FOUND"
_TEMPLATE = "{{"
_SUBFLOW = "subflow"
_CHILD = "child"


class BuildPath(Protocol):
    """What the opener uses of ``WorkflowRuntime``."""

    loader: Any
    blueprints: Any
    graphs: Any

    def tool_catalog_version(self) -> str: ...


@dataclass
class _Children:
    until: float
    documents: dict[str, LoadedDocument]


def static_children(parent: Blueprint) -> list[str]:
    found: list[str] = []
    for spec in parent.nodes.values():
        child = getattr(spec.config, "agentic_workflow_id", None)
        if (
            spec.kind == _SUBFLOW
            and isinstance(child, str)
            and child
            and _TEMPLATE not in child
            and child not in found
        ):
            found.append(child)
    return found


class SubflowOpener:
    def __init__(
        self, cache_seconds: float, entries: int, monotonic: Callable[[], float] = time.monotonic
    ) -> None:
        self._cache_seconds = cache_seconds
        self._monotonic = monotonic
        self._children: LruCache[str, _Children] = LruCache(entries)
        self._runtime: BuildPath | None = None

    def bind(self, runtime: BuildPath) -> None:
        self._runtime = runtime

    async def open(
        self, parent: Blueprint, child_id: str, documents: RunDocuments | None = None
    ) -> ChildWorkflow:
        runtime = self._runtime
        if runtime is None:
            raise RuntimeError("SubflowOpener is not bound to a WorkflowRuntime")
        pinned = documents.pinned(child_id) if documents is not None else None
        if pinned is not None:  # a resumed run: the child it paused with (H-09)
            loaded = runtime.loader.from_stored(pinned, child_id)
        else:
            loaded = (await self._children_of(parent, runtime)).get(child_id) or await self._one(
                child_id, runtime
            )
        if documents is not None:
            documents.add(child_id, loaded.stored)
        blueprint = runtime.blueprints.get(loaded)
        graph = runtime.graphs.get(
            blueprint, role=_CHILD, tool_catalog_version=runtime.tool_catalog_version()
        )
        return ChildWorkflow(
            blueprint=blueprint, graph=graph, variables=frozenset(loaded.document.variable_definitions())
        )

    async def _children_of(self, parent: Blueprint, runtime: BuildPath) -> dict[str, LoadedDocument]:
        now = self._monotonic()
        cached = self._children.get(parent.revision)
        if cached is not None and cached.until > now:
            return cached.documents
        ids = static_children(parent)
        documents: dict[str, LoadedDocument] = {}
        if ids:
            try:
                documents = {d.agentic_workflow_id: d for d in await runtime.loader.load_exports(ids)}
            except ZonicError as error:
                if error.code != NOT_FOUND:
                    raise  # one missing child: the others are read when opened
        self._children.put(parent.revision, _Children(until=now + self._cache_seconds, documents=documents))
        return documents

    @staticmethod
    async def _one(child_id: str, runtime: BuildPath) -> LoadedDocument:
        [found] = await runtime.loader.load_exports([child_id])
        return found  # type: ignore[no-any-return]
