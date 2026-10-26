"""SP-9: node name constraints (design Section 6.9's escaping rule depends on them).

Each candidate name is added to a one-node graph, compiled and run with a checkpointer,
and run again as the node that invokes a child graph so the name appears in a namespace.
The spike records which names LangGraph rejects, at which step, and whether accepted
names survive in stream namespaces and checkpoint namespaces unchanged.
"""

from __future__ import annotations

from typing import Any, TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

CANDIDATES = [
    "agent-1",
    "agent_1",
    "Agent 1",
    "node.with.dots",
    "a:b",
    "a|b",
    "a/b",
    "ünïcødé-名前",
    "x" * 300,
    "__start__",
    "__end__",
    "__interrupt__",
    "__pregel_push",
    "branch:to:x",
    "value",
    "",
]


class S(TypedDict, total=False):
    value: int


def _child() -> Any:
    g = StateGraph(S)
    g.add_node("leaf", lambda s: {"value": 2})
    g.add_edge(START, "leaf")
    g.add_edge("leaf", END)
    return g.compile()


def _try(name: str) -> str:
    child = _child()

    async def node(state: S) -> S:
        return await child.ainvoke({"value": 1})

    stage = "add_node"
    try:
        g = StateGraph(S)
        g.add_node(name, node)
        stage = "edges"
        g.add_edge(START, name)
        g.add_edge(name, END)
        stage = "compile"
        graph = g.compile(checkpointer=InMemorySaver())
    except Exception as exc:
        return f"rejected at {stage}: {type(exc).__name__}: {str(exc)[:90]}"
    return "compiled"


async def _run(name: str) -> str:
    child = _child()

    async def node(state: S) -> S:
        return await child.ainvoke({"value": 1})

    g = StateGraph(S)
    g.add_node(name, node)
    g.add_edge(START, name)
    g.add_edge(name, END)
    graph = g.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "t"}}
    try:
        namespaces = []
        async for ns, _chunk in graph.astream({"value": 0}, config, stream_mode="values", subgraphs=True):
            if ns:
                namespaces.append(ns[0])
        state = await graph.aget_state(config)
    except Exception as exc:
        return f"run failed: {type(exc).__name__}: {str(exc)[:90]}"
    first = namespaces[0] if namespaces else ""
    segment_name = first.rsplit(":", 1)[0] if first else ""
    return f"ran value={state.values['value']} namespace-name-intact={segment_name == name}"


@pytest.mark.parametrize("name", CANDIDATES, ids=[repr(c[:20]) for c in CANDIDATES])
async def test_node_name(name: str) -> None:
    compiled = _try(name)
    result = await _run(name) if compiled == "compiled" else ""
    print(f"\nSP-9 {name[:24]!a:<34} {compiled} {result}")
