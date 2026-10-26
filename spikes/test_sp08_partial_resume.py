"""SP-8: resuming a subset of several pending interrupts.

Two parallel branches pause in the same superstep (one directly, one inside a child graph
invoked in its node). The client answers only one. Questions:
1. Does ``Command(resume={interrupt_id: value})`` run the answered branch and leave the
   other pending, with the same interrupt ID?
2. Does the answered branch's result survive the partial resume through a TurnSaver
   bundle (export, import into a fresh saver), and does the second answer finish the run?
3. Is an unanswered branch re-run (side effects before ``interrupt()``) on each resume?
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from spikes.turnsaver import TurnSaver

RUNS: dict[str, int] = {"a": 0, "b": 0}


class S(TypedDict, total=False):
    log: Annotated[list[str], operator.add]


class C(TypedDict, total=False):
    answer: Any


def _child() -> Any:
    def ask(state: C) -> C:
        return {"answer": interrupt({"prompt": "approve b?"})}

    g = StateGraph(C)
    g.add_node("ask", ask)
    g.add_edge(START, "ask")
    g.add_edge("ask", END)
    return g.compile()


def _graph(saver: TurnSaver) -> Any:
    child = _child()

    def approve_a(state: S) -> S:
        RUNS["a"] += 1
        return {"log": [f"a={interrupt({'prompt': 'approve a?'})}"]}

    async def approve_b(state: S) -> S:
        RUNS["b"] += 1
        out = await child.ainvoke({})
        return {"log": [f"b={out['answer']}"]}

    g = StateGraph(S)
    g.add_node("fork", lambda s: {"log": ["fork"]})
    g.add_node("approve_a", approve_a)
    g.add_node("approve_b", approve_b)
    g.add_node("join", lambda s: {"log": ["join"]}, defer=True)
    g.add_edge(START, "fork")
    g.add_edge("fork", "approve_a")
    g.add_edge("fork", "approve_b")
    g.add_edge("approve_a", "join")
    g.add_edge("approve_b", "join")
    g.add_edge("join", END)
    return g.compile(checkpointer=saver)


def _prompts(out: dict[str, Any]) -> dict[str, str]:
    return {i.value["prompt"]: i.id for i in out.get("__interrupt__", [])}


async def test_resume_one_of_two_pending_interrupts_across_bundles() -> None:
    RUNS.update(a=0, b=0)
    config = {"configurable": {"thread_id": "sp8"}}
    saver = TurnSaver()
    out = await _graph(saver).ainvoke({"log": []}, config, durability="exit")
    pending = _prompts(out)
    print("\nSP-8.1 pending:", pending)
    assert set(pending) == {"approve a?", "approve b?"}

    saver = TurnSaver.from_bundle(saver.export_bundle("sp8"))
    out = await _graph(saver).ainvoke(
        Command(resume={pending["approve a?"]: "yes"}), config, durability="exit"
    )
    still = _prompts(out)
    print("SP-8.2 after answering a:", still, "| log", out.get("log"), "| node runs", dict(RUNS))
    assert set(still) == {"approve b?"}
    assert still["approve b?"] == pending["approve b?"]

    saver = TurnSaver.from_bundle(saver.export_bundle("sp8"))
    out = await _graph(saver).ainvoke(Command(resume={still["approve b?"]: "no"}), config, durability="exit")
    print("SP-8.3 after answering b:", out["log"], "| node runs", dict(RUNS))
    assert "__interrupt__" not in out
    assert sorted(out["log"]) == sorted(["fork", "a=yes", "b=no", "join"])


async def test_resume_with_a_plain_value_when_two_are_pending() -> None:
    """What a single-value resume does when two interrupts are pending (the API must not send it)."""
    config = {"configurable": {"thread_id": "sp8-plain"}}
    saver = TurnSaver()
    await _graph(saver).ainvoke({"log": []}, config, durability="exit")
    try:
        out = await _graph(saver).ainvoke(Command(resume="yes"), config, durability="exit")
        print("\nSP-8.4 plain resume with two pending:", out.get("log"), _prompts(out))
    except Exception as exc:
        print(f"\nSP-8.4 plain resume with two pending raised {type(exc).__name__}: {exc}")
