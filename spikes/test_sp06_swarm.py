"""SP-6: swarm handoffs with ``Command(graph=Command.PARENT)`` from agents invoked in member nodes.

The design's swarm node invokes a swarm graph whose member nodes each invoke a
``create_agent`` graph; a handoff tool returns ``Command(goto=<member>, graph=Command.PARENT)``.

Questions:
1. Does the handoff, raised by a tool inside the agent graph invoked inside the member node,
   route the *swarm* graph to the next member, with the shared message history?
2. An interrupt inside a member resumes (new process not needed here: SP-2 covers that).
3. The swarm's step limit (``recursion_limit`` on the swarm invocation) stops endless handoffs.
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.messages import AnyMessage, HumanMessage, ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph, add_messages
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from tests.fakes.scripted_chat_model import ScriptedChatModel, Turn


class Swarm(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    active: str
    handoffs: int


def handoff(target: str) -> Any:
    @tool(f"transfer_to_{target}")
    def transfer(
        state: Annotated[dict[str, Any], InjectedState],
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> Command[str]:
        """Hand the conversation to another member."""
        done = ToolMessage(f"transferred to {target}", tool_call_id=tool_call_id)
        return Command(
            goto=target,
            graph=Command.PARENT,
            update={"messages": [*state["messages"], done], "active": target},
        )

    return transfer


def _swarm(models: dict[str, ScriptedChatModel], *, gate_bob: bool = False) -> Any:
    members = list(models)
    agents = {
        name: create_agent(
            models[name],
            tools=[handoff(other) for other in members if other != name],
            middleware=[HumanInTheLoopMiddleware(interrupt_on={"transfer_to_alice": True})]
            if gate_bob and name == "bob"
            else [],
        )
        for name in members
    }

    def member(name: str) -> Any:
        async def run(state: Swarm) -> Swarm:
            out = await agents[name].ainvoke({"messages": state["messages"]})
            return {"messages": out["messages"], "active": name}

        return run

    g = StateGraph(Swarm)
    for name in members:
        g.add_node(name, member(name), destinations=tuple(m for m in members if m != name) + (END,))
    g.add_edge(START, members[0])
    for name in members:
        g.add_edge(name, END)
    return g.compile()


class Root(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    answer: str
    error: str


def _root(swarm: Any, limit: int = 50) -> Any:
    async def swarm_1(state: Root) -> Root:
        try:
            out = await swarm.ainvoke({"messages": state["messages"]}, {"recursion_limit": limit})
        except GraphRecursionError as exc:
            return {"error": f"step limit: {type(exc).__name__}"}
        return {"messages": out["messages"], "answer": out["messages"][-1].text}

    g = StateGraph(Root)
    g.add_node("swarm_1", swarm_1)
    g.add_edge(START, "swarm_1")
    g.add_edge("swarm_1", END)
    return g.compile(checkpointer=InMemorySaver())


async def test_handoff_routes_the_swarm_graph_with_shared_history() -> None:
    alice = ScriptedChatModel(script=[Turn.call("transfer_to_bob", {})])
    bob = ScriptedChatModel(script=[Turn.say("Bob here: done.")])
    graph = _root(_swarm({"alice": alice, "bob": bob}))
    out = await graph.ainvoke({"messages": [HumanMessage("need bob")]}, {"configurable": {"thread_id": "s1"}})
    print("\nSP-6.1 messages:", [(type(m).__name__, m.text[:30]) for m in out["messages"]])
    assert out["answer"] == "Bob here: done."
    seen_by_bob = [type(m).__name__ for m in bob.requests[0].messages]
    print("SP-6.1 bob saw:", seen_by_bob)
    assert seen_by_bob == ["HumanMessage", "AIMessage", "ToolMessage"]


async def test_interrupt_inside_a_member_resumes() -> None:
    alice = ScriptedChatModel(script=[Turn.call("transfer_to_bob", {}), Turn.say("Alice again: finished.")])
    bob = ScriptedChatModel(script=[Turn.call("transfer_to_alice", {})])
    graph = _root(_swarm({"alice": alice, "bob": bob}, gate_bob=True))
    config = {"configurable": {"thread_id": "s2"}}
    out = await graph.ainvoke({"messages": [HumanMessage("go")]}, config, durability="exit")
    (pending,) = out["__interrupt__"]
    print("\nSP-6.2 interrupt inside bob:", pending.value["action_requests"][0]["name"])
    out = await graph.ainvoke(Command(resume={"decisions": [{"type": "approve"}]}), config, durability="exit")
    print(
        "SP-6.2 after resume:",
        out["answer"],
        "| model calls alice",
        len(alice.requests),
        "bob",
        len(bob.requests),
    )
    assert out["answer"] == "Alice again: finished."
    assert len(bob.requests) == 1  # bob's turn is not repeated on resume


@pytest.mark.parametrize("limit", [5, 11])
async def test_step_limit_stops_endless_handoffs(limit: int) -> None:
    alice = ScriptedChatModel(script=[Turn.call("transfer_to_bob", {})], repeat_last=True)
    bob = ScriptedChatModel(script=[Turn.call("transfer_to_alice", {})], repeat_last=True)
    graph = _root(_swarm({"alice": alice, "bob": bob}), limit=limit)
    out = await graph.ainvoke(
        {"messages": [HumanMessage("loop")]}, {"configurable": {"thread_id": f"s3-{limit}"}}
    )
    print(
        f"\nSP-6.3 recursion_limit={limit}: {out.get('error')} after {len(alice.requests)} + {len(bob.requests)} turns"
    )
    assert out["error"].startswith("step limit")
    assert len(alice.requests) + len(bob.requests) == limit
