"""SP-10 graphs, shared by the spike and the resume subprocess (``python -m spikes.sp10_resume``).

``root_chain``: six steps in the root graph. ``child_chain``: the root node ``sub`` invokes a
five-step child graph. ``parallel``: two slow nodes in one superstep, then a join. Every
step appends to ``log``, so repeated work after a resume shows up as a duplicate.
"""

from __future__ import annotations

import asyncio
import operator
import os
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

STEP_S = 0.05


class S(TypedDict, total=False):
    log: Annotated[list[str], operator.add]


def _step(name: str) -> Any:
    async def run(state: S) -> S:
        await asyncio.sleep(STEP_S)
        trace = os.environ.get("SP10_TRACE")
        if trace:  # every execution, across both processes
            with open(trace, "a", encoding="utf-8") as f:
                f.write(f"{os.getpid()} {name}\n")
        return {"log": [name]}

    return run


def _chain(names: list[str]) -> StateGraph[S]:
    g = StateGraph(S)
    for name in names:
        g.add_node(name, _step(name))
    g.add_edge(START, names[0])
    for a, b in zip(names, names[1:], strict=False):
        g.add_edge(a, b)
    g.add_edge(names[-1], END)
    return g


def root_chain(saver: BaseCheckpointSaver[Any]) -> Any:
    return _chain([f"r{i}" for i in range(1, 7)]).compile(checkpointer=saver)


def child_chain(saver: BaseCheckpointSaver[Any]) -> Any:
    child = _chain([f"c{i}" for i in range(1, 6)]).compile()

    async def sub(state: S) -> S:
        out = await child.ainvoke({"log": []})
        return {"log": ["sub", *out["log"]]}

    g = StateGraph(S)
    g.add_node("before", _step("before"))
    g.add_node("sub", sub)
    g.add_node("after", _step("after"))
    g.add_edge(START, "before")
    g.add_edge("before", "sub")
    g.add_edge("sub", "after")
    g.add_edge("after", END)
    return g.compile(checkpointer=saver)


def parallel(saver: BaseCheckpointSaver[Any]) -> Any:
    g = StateGraph(S)
    g.add_node("fork", _step("fork"))
    g.add_node("slow_a", _step("slow_a"))
    g.add_node("slow_b", _step("slow_b"))
    g.add_node("join", _step("join"))
    g.add_edge(START, "fork")
    g.add_edge("fork", "slow_a")
    g.add_edge("fork", "slow_b")
    g.add_edge(["slow_a", "slow_b"], "join")
    g.add_edge("join", END)
    return g.compile(checkpointer=saver)


FLOWS = {"root": root_chain, "child": child_chain, "parallel": parallel}
