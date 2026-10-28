"""Handles to ``Command(goto=...)`` (design 6.4; plan P1-09).

Every node ends the same way: its outcome names the handles it activates and the edge
index turns them into targets. ``*`` activates every outgoing edge. An activated handle
without an edge ends that path when the manifest marks it optional or the node has no
outgoing edge at all (a sink); otherwise the run fails with ``ROUTE_UNRESOLVED`` (case
D-08: an unconnected branch fails only when it is taken). An empty ``goto`` ends the
path; the run ends when no task is pending.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langgraph.types import Command

from zonic_runtime.errors import ZonicError
from zonic_runtime.models.blueprint import Blueprint, NodeSpec
from zonic_runtime.models.engine.records import ALL_HANDLES


def targets(blueprint: Blueprint, spec: NodeSpec, activate: Sequence[str]) -> list[str]:
    """Builder node IDs to run next, in edge order, each once."""
    index = blueprint.edge_index
    if ALL_HANDLES in activate:
        return index.all_targets(spec.node_id)
    found: list[str] = []
    sink = not index.handles(spec.node_id)
    for handle in activate:
        reached = index.targets(spec.node_id, handle)
        if not reached and not sink and handle not in spec.optional_handles:
            raise ZonicError("ROUTE_UNRESOLVED", node_id=spec.node_id, handle=handle)
        found += [t for t in reached if t not in found]
    return found


def command(
    blueprint: Blueprint, spec: NodeSpec, activate: Sequence[str], update: dict[str, Any]
) -> Command[Any]:
    """The node's return value: its state update and the LangGraph names of its targets."""
    goto = [blueprint.nodes[t].graph_name for t in targets(blueprint, spec, activate)]
    return Command(update=update, goto=goto)
