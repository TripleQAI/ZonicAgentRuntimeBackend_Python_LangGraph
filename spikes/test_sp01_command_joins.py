"""SP-1: ``Command`` routing joins, ``defer`` with ``Command``, superstep overhead, recursion limit.

Questions (design 20.1, plan P0-10 item 1, DR-13):
1. Two nodes in one superstep both ``Command(goto="join")``: does ``join`` run once?
2. Does ``defer=True`` hold a ``Command``-triggered node until every other branch is done?
3. Same-superstep double trigger from a static edge and a ``Command`` to the same target.
4. Per-superstep overhead with ``durability="exit"`` versus no checkpointer and "async".
5. The installed default recursion limit.
"""

from __future__ import annotations

import operator
import time
from typing import Annotated, Literal, TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command


class S(TypedDict, total=False):
    log: Annotated[list[str], operator.add]
    n: int


def visit(name: str, goto: str | list[str] | None = None):  # type: ignore[no-untyped-def]
    def node(state: S) -> Command[str] | dict[str, list[str]]:
        if goto is None:
            return {"log": [name]}
        return Command(update={"log": [name]}, goto=goto)

    node.__name__ = name
    return node


async def test_two_commands_to_one_target_in_one_superstep_run_it_once() -> None:
    g = StateGraph(S)
    g.add_node("start", visit("start", ["a", "b"]), destinations=("a", "b"))
    g.add_node("a", visit("a", "join"), destinations=("join",))
    g.add_node("b", visit("b", "join"), destinations=("join",))
    g.add_node("join", visit("join"))
    g.add_edge(START, "start")
    g.add_edge("join", END)
    out = await g.compile().ainvoke({"log": []})
    print("\nSP-1.1 log:", out["log"])
    assert out["log"].count("join") == 1
    assert out["log"][:1] == ["start"] and set(out["log"][1:3]) == {"a", "b"}


async def test_unequal_branches_without_defer_join_early() -> None:
    """The baseline that makes ``defer`` necessary: join runs when the short branch arrives."""
    g = StateGraph(S)
    g.add_node("start", visit("start", ["a", "b1"]), destinations=("a", "b1"))
    g.add_node("a", visit("a", "join"), destinations=("join",))
    g.add_node("b1", visit("b1", "b2"), destinations=("b2",))
    g.add_node("b2", visit("b2", "join"), destinations=("join",))
    g.add_node("join", visit("join"))
    g.add_edge(START, "start")
    g.add_edge("join", END)
    out = await g.compile().ainvoke({"log": []})
    print("\nSP-1.2 without defer:", out["log"])
    assert out["log"].count("join") == 2


async def test_defer_holds_a_command_triggered_join_until_branches_finish() -> None:
    g = StateGraph(S)
    g.add_node("start", visit("start", ["a", "b1"]), destinations=("a", "b1"))
    g.add_node("a", visit("a", "join"), destinations=("join",))
    g.add_node("b1", visit("b1", "b2"), destinations=("b2",))
    g.add_node("b2", visit("b2", "b3"), destinations=("b3",))
    g.add_node("b3", visit("b3", "join"), destinations=("join",))
    g.add_node("join", visit("join"), defer=True)
    g.add_edge(START, "start")
    g.add_edge("join", END)
    out = await g.compile().ainvoke({"log": []})
    print("\nSP-1.3 with defer:", out["log"])
    assert out["log"].count("join") == 1
    assert out["log"][-1] == "join"


async def test_defer_also_waits_for_branches_that_never_reach_the_join() -> None:
    """``defer`` is a run-level barrier: it waits for every pending task, not just its sources."""
    g = StateGraph(S)
    g.add_node("start", visit("start", ["a", "c1"]), destinations=("a", "c1"))
    g.add_node("a", visit("a", "join"), destinations=("join",))
    g.add_node("c1", visit("c1", "c2"), destinations=("c2",))
    g.add_node("c2", visit("c2", "c3"), destinations=("c3",))
    g.add_node("c3", visit("c3"))
    g.add_node("join", visit("join"), defer=True)
    g.add_edge(START, "start")
    g.add_edge("c3", END)
    g.add_edge("join", END)
    out = await g.compile().ainvoke({"log": []})
    print("\nSP-1.3b unrelated branch:", out["log"])
    assert out["log"][-1] == "join"


async def test_list_form_barrier_waits_for_named_sources_only() -> None:
    """The fallback: ``add_edge([a, b2], join)`` joins exactly its sources; an unrelated branch runs on."""
    g = StateGraph(S)
    g.add_node("start", visit("start"))
    for name in ("a", "b1", "b2", "c1", "c2", "c3", "c4", "join"):
        g.add_node(name, visit(name))
    g.add_edge(START, "start")
    g.add_edge("start", "a")
    g.add_edge("start", "b1")
    g.add_edge("start", "c1")
    g.add_edge("b1", "b2")
    g.add_edge("c1", "c2")
    g.add_edge("c2", "c3")
    g.add_edge("c3", "c4")
    g.add_edge(["a", "b2"], "join")
    g.add_edge("join", END)
    g.add_edge("c4", END)
    out = await g.compile().ainvoke({"log": []})
    print("\nSP-1.3c list-form barrier:", out["log"])
    assert out["log"].count("join") == 1
    assert out["log"].index("join") < out["log"].index("c4")


async def test_list_form_barrier_with_an_untaken_source_never_fires() -> None:
    """Why the compiler cannot use list-form barriers after exclusive branches."""

    def route(state: S) -> Command[Literal["a"]]:
        return Command(update={"log": ["route"]}, goto="a")  # "b" is never taken

    g = StateGraph(S)
    g.add_node("route", route)
    g.add_node("a", visit("a"))
    g.add_node("b", visit("b"))
    g.add_node("join", visit("join"))
    g.add_edge(START, "route")
    g.add_edge(["a", "b"], "join")
    g.add_edge("join", END)
    out = await g.compile().ainvoke({"log": []})
    print("\nSP-1.3d barrier with untaken source:", out["log"])
    assert "join" not in out["log"]


async def test_static_edge_and_command_to_the_same_target_in_one_superstep() -> None:
    g = StateGraph(S)
    g.add_node("start", visit("start", ["a", "b"]), destinations=("a", "b"))
    g.add_node("a", visit("a"))
    g.add_node("b", visit("b", "join"), destinations=("join",))
    g.add_node("join", visit("join"))
    g.add_edge(START, "start")
    g.add_edge("a", "join")
    g.add_edge("join", END)
    out = await g.compile().ainvoke({"log": []})
    print("\nSP-1.4 edge + Command:", out["log"])
    assert out["log"].count("join") == 1


async def test_defer_join_inside_a_loop_runs_once_per_round() -> None:
    """A deferred join in a cycle: each round's join fires once, after that round's branches."""

    def start(state: S) -> Command[Literal["a", "b1"]]:
        return Command(update={"log": ["start"]}, goto=["a", "b1"])

    def join(state: S) -> Command[Literal["start", "__end__"]]:
        rounds = state.get("n", 0) + 1
        return Command(update={"log": ["join"], "n": rounds}, goto="start" if rounds < 3 else END)

    g = StateGraph(S)
    g.add_node("start", start)
    g.add_node("a", visit("a", "join"), destinations=("join",))
    g.add_node("b1", visit("b1", "b2"), destinations=("b2",))
    g.add_node("b2", visit("b2", "join"), destinations=("join",))
    g.add_node("join", join, defer=True)
    g.add_edge(START, "start")
    out = await g.compile().ainvoke({"log": [], "n": 0})
    print("\nSP-1.5 deferred join in a loop:", out["log"])
    assert out["log"].count("join") == 3 and out["n"] == 3


def _chain(steps: int) -> StateGraph[S]:
    def tick(state: S) -> Command[Literal["tick", "__end__"]]:
        n = state.get("n", 0) + 1
        return Command(update={"n": n}, goto="tick" if n < steps else END)

    g = StateGraph(S)
    g.add_node("tick", tick)
    g.add_edge(START, "tick")
    return g


@pytest.mark.parametrize("mode", ["none", "exit", "async", "sync"])
async def test_superstep_overhead(mode: str) -> None:
    steps = 400
    g = _chain(steps)
    graph = g.compile() if mode == "none" else g.compile(checkpointer=InMemorySaver())
    config = {"recursion_limit": steps + 5, "configurable": {"thread_id": "t"}}
    kwargs = {} if mode == "none" else {"durability": mode}
    await graph.ainvoke({"n": 0}, config, **kwargs)  # warm up
    best = float("inf")
    for i in range(3):
        config["configurable"]["thread_id"] = f"t{i}"
        started = time.perf_counter()
        out = await graph.ainvoke({"n": 0}, config, **kwargs)
        best = min(best, time.perf_counter() - started)
    assert out["n"] == steps
    print(f"\nSP-1.6 durability={mode}: {best / steps * 1e6:.0f} us per superstep ({steps} steps)")


async def test_default_recursion_limit_and_explicit_limit() -> None:
    from langgraph._internal._config import DEFAULT_RECURSION_LIMIT

    print(f"\nSP-1.7 installed default recursion limit: {int(DEFAULT_RECURSION_LIMIT)}")
    graph = _chain(10_000_000).compile()
    with pytest.raises(GraphRecursionError):
        await graph.ainvoke({"n": 0}, {"recursion_limit": 25})
    assert int(DEFAULT_RECURSION_LIMIT) == 10007
