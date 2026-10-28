"""Sub-flow references across documents (design 6.7): preloading, ``SUBFLOW_CYCLE``, depth.

A blueprint lists its children (``Blueprint.children``, from ``node_semantics.childReference``).
``subflow_closure`` walks them from a root, loading each level with one call, and fails
before any node runs when a workflow reaches itself (``A -> B -> A``) or the chain is deeper
than ``settings.subflow.maxDepth``. Children whose ID is a template are resolved at run time
and are not in the list.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence

from zonic_runtime.errors import ZonicError
from zonic_runtime.models.blueprint import Blueprint

LoadBlueprints = Callable[[Sequence[str]], Awaitable[list[Blueprint]]]


async def subflow_closure(root: Blueprint, load: LoadBlueprints, max_depth: int) -> dict[str, Blueprint]:
    """Every blueprint the root can reach through sub-flow nodes, by workflow ID."""
    known: dict[str, Blueprint] = {root.workflow_id: root}

    async def visit(blueprint: Blueprint, path: list[str]) -> None:
        missing = [c for c in blueprint.children if c not in known]
        if missing:
            for loaded in await load(missing):
                known[loaded.workflow_id] = loaded
        for child in blueprint.children:
            if child in path:
                raise ZonicError("SUBFLOW_CYCLE", path=[*path, child])
            if len(path) > max_depth:
                raise ZonicError("SUBFLOW_DEPTH_EXCEEDED", path=[*path, child], max_depth=max_depth)
            await visit(known[child], [*path, child])

    await visit(root, [root.workflow_id])
    return known
