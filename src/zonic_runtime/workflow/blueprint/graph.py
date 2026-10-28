"""Graph analysis of the compiled nodes (design 6.2, 6.5, 6.6, 6.9).

* Strongly connected components (Tarjan, iterative), ported from the Node.js runtime's
  ``loop-analysis.ts``: a component with a cycle is one loop.
* Fan-in: a node is a barrier join when it can be reached from two different targets of
  one parallel fan-out. A fan-out is one ``(node, handle)`` with two or more targets, since
  activating a handle schedules all its targets in the same superstep; different handles of
  one node are exclusive (a node activates one handle per visit). The two paths diverge at
  the fan-out, so an exclusive branch later on either path does not remove the barrier: the
  branch may be taken, and the join would then run once per arriving superstep.
* LangGraph node names (spike SP-9): builder IDs are used unchanged unless they contain
  ``:`` or ``|`` or start with ``__``; those are escaped deterministically.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence

Edges = Mapping[str, Mapping[str, Sequence[str]]]  # source -> handle -> targets

_RESERVED = re.compile(r"[:|]")


def successors(edges: Edges, node: str) -> list[str]:
    seen: list[str] = []
    for targets in edges.get(node, {}).values():
        seen += [t for t in targets if t not in seen]
    return seen


def strongly_connected(nodes: Sequence[str], edges: Edges) -> list[list[str]]:
    """Tarjan's components, each in document order, ordered by their first node."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[list[str]] = []
    counter = 0
    for root in nodes:
        if root in index:
            continue
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        work: list[tuple[str, list[str], int]] = [(root, successors(edges, root), 0)]
        while work:
            node, targets, nxt = work[-1]
            if nxt < len(targets):
                work[-1] = (node, targets, nxt + 1)
                target = targets[nxt]
                if target not in index:
                    index[target] = low[target] = counter
                    counter += 1
                    stack.append(target)
                    on_stack.add(target)
                    work.append((target, successors(edges, target), 0))
                elif target in on_stack:
                    low[node] = min(low[node], index[target])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                components.append(component)
    order = {node: i for i, node in enumerate(nodes)}
    ordered = [sorted(c, key=order.__getitem__) for c in components]
    return sorted(ordered, key=lambda c: order[c[0]])


def cyclic(component: Sequence[str], edges: Edges) -> bool:
    return len(component) > 1 or component[0] in successors(edges, component[0])


def _reach(start: str, edges: Edges, blocked: str) -> set[str]:
    reached = {start}
    queue = [start]
    while queue:
        for target in successors(edges, queue.pop()):
            if target != blocked and target not in reached:
                reached.add(target)
                queue.append(target)
    return reached


def predecessors(nodes: Iterable[str], edges: Edges) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {n: [] for n in nodes}
    for source, handles in edges.items():
        for targets in handles.values():
            for target in targets:
                if source not in found.setdefault(target, []):
                    found[target].append(source)
    return found


def fan_in_joins(nodes: Sequence[str], edges: Edges) -> set[str]:
    """Nodes reached from two or more targets of one parallel fan-out, with two or more
    distinct predecessors on those paths (the case of design 6.5 that would run twice)."""
    incoming = predecessors(nodes, edges)
    joins: set[str] = set()
    for fan_out in nodes:
        for targets in edges.get(fan_out, {}).values():
            distinct = list(dict.fromkeys(targets))
            if len(distinct) < 2:
                continue
            reaches = [_reach(t, edges, fan_out) for t in distinct]
            union = set().union(*reaches) | {fan_out}
            for node in union - {fan_out}:
                arriving = sum(1 for r in reaches if node in r)
                sources = [p for p in incoming.get(node, []) if p in union]
                if arriving >= 2 and len(sources) >= 2:
                    joins.add(node)
    return joins


def graph_name(node_id: str) -> str:
    """The LangGraph node name of a builder node ID (SP-9)."""
    if not _RESERVED.search(node_id) and not node_id.startswith("__"):
        return node_id
    readable = _RESERVED.sub("_", node_id).lstrip("_") or "node"
    return f"{readable}~{hashlib.sha256(node_id.encode('utf-8')).hexdigest()[:8]}"
